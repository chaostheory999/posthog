import functools
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, cast
from unittest import mock

import aioboto3
import deltalake
import posthoganalytics
import psycopg
import pytest
import pytest_asyncio
import s3fs
from asgiref.sync import sync_to_async
from deltalake import DeltaTable
from django.conf import settings
from django.test import override_settings
from dlt.common.configuration.specs.aws_credentials import AwsCredentials
from dlt.sources.helpers.rest_client.client import RESTClient
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from posthog.constants import DATA_WAREHOUSE_TASK_QUEUE
from posthog.hogql.modifiers import create_default_modifiers_for_team
from posthog.hogql.query import execute_hogql_query
from posthog.hogql_queries.insights.funnels.funnel import Funnel
from posthog.hogql_queries.insights.funnels.funnel_query_context import (
    FunnelQueryContext,
)
from posthog.models.team.team import Team
from posthog.schema import (
    BreakdownFilter,
    BreakdownType,
    EventsNode,
    FunnelsQuery,
    HogQLQueryModifiers,
    PersonsOnEventsMode,
)
from posthog.temporal.data_imports.pipelines.pipeline.consts import PARTITION_KEY
from posthog.temporal.data_imports.row_tracking import get_rows
from posthog.temporal.data_imports.settings import ACTIVITIES
from posthog.temporal.data_imports.external_data_job import ExternalDataJobWorkflow
from posthog.temporal.data_imports.pipelines.pipeline.pipeline import PipelineNonDLT
from posthog.temporal.utils import ExternalDataWorkflowInputs
from posthog.warehouse.models import (
    ExternalDataJob,
    ExternalDataSchema,
    ExternalDataSource,
)
from posthog.warehouse.models.external_data_job import get_latest_run_if_exists
from posthog.warehouse.models.external_table_definitions import external_tables
from posthog.warehouse.models.join import DataWarehouseJoin
from posthog.temporal.data_imports.pipelines.stripe.constants import (
    BALANCE_TRANSACTION_RESOURCE_NAME as STRIPE_BALANCE_TRANSACTION_RESOURCE_NAME,
    CHARGE_RESOURCE_NAME as STRIPE_CHARGE_RESOURCE_NAME,
    CUSTOMER_RESOURCE_NAME as STRIPE_CUSTOMER_RESOURCE_NAME,
    INVOICE_RESOURCE_NAME as STRIPE_INVOICE_RESOURCE_NAME,
    PRICE_RESOURCE_NAME as STRIPE_PRICE_RESOURCE_NAME,
    PRODUCT_RESOURCE_NAME as STRIPE_PRODUCT_RESOURCE_NAME,
    SUBSCRIPTION_RESOURCE_NAME as STRIPE_SUBSCRIPTION_RESOURCE_NAME,
)

BUCKET_NAME = "test-pipeline"
SESSION = aioboto3.Session()
create_test_client = functools.partial(SESSION.client, endpoint_url=settings.OBJECT_STORAGE_ENDPOINT)


@pytest.fixture
def postgres_config():
    return {
        "user": settings.PG_USER,
        "password": settings.PG_PASSWORD,
        "database": "external_data_database",
        "schema": "external_data_schema",
        "host": settings.PG_HOST,
        "port": int(settings.PG_PORT),
    }


@pytest_asyncio.fixture
async def postgres_connection(postgres_config, setup_postgres_test_db):
    connection = await psycopg.AsyncConnection.connect(
        user=postgres_config["user"],
        password=postgres_config["password"],
        dbname=postgres_config["database"],
        host=postgres_config["host"],
        port=postgres_config["port"],
    )

    yield connection

    await connection.close()


@pytest_asyncio.fixture(autouse=True)
async def minio_client():
    """Manage an S3 client to interact with a MinIO bucket.

    Yields the client after creating a bucket. Upon resuming, we delete
    the contents and the bucket itself.
    """
    async with create_test_client(
        "s3",
        aws_access_key_id=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
    ) as minio_client:
        try:
            await minio_client.head_bucket(Bucket=BUCKET_NAME)
        except:
            await minio_client.create_bucket(Bucket=BUCKET_NAME)

        yield minio_client


async def _run(
    team: Team,
    schema_name: str,
    table_name: str,
    source_type: str,
    job_inputs: dict[str, str],
    mock_data_response: Any,
    sync_type: Optional[ExternalDataSchema.SyncType] = None,
    sync_type_config: Optional[dict] = None,
    billable: Optional[bool] = None,
    ignore_assertions: Optional[bool] = False,
):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type=source_type,
        revenue_analytics_enabled=source_type == ExternalDataSource.Type.STRIPE,
        job_inputs=job_inputs,
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name=schema_name,
        team_id=team.pk,
        source_id=source.pk,
        sync_type=sync_type,
        sync_type_config=sync_type_config or {},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
        billable=billable if billable is not None else True,
    )

    with (
        mock.patch(
            "posthog.temporal.data_imports.pipelines.pipeline.pipeline.trigger_compaction_job"
        ) as mock_trigger_compaction_job,
        mock.patch(
            "posthog.temporal.data_imports.external_data_job.get_data_import_finished_metric"
        ) as mock_get_data_import_finished_metric,
    ):
        await _execute_run(workflow_id, inputs, mock_data_response)

    if not ignore_assertions:
        run: ExternalDataJob = await get_latest_run_if_exists(team_id=team.pk, pipeline_id=source.pk)

        assert run is not None
        assert run.status == ExternalDataJob.Status.COMPLETED

        mock_trigger_compaction_job.assert_called()
        mock_get_data_import_finished_metric.assert_called_with(
            source_type=source_type, status=ExternalDataJob.Status.COMPLETED.lower()
        )

        await sync_to_async(schema.refresh_from_db)()

        assert schema.last_synced_at == run.created_at

        res = await sync_to_async(execute_hogql_query)(f"SELECT * FROM {table_name}", team)
        assert len(res.results) == 1

        for name, field in external_tables.get(table_name, {}).items():
            if field.hidden:
                continue
            assert name in (res.columns or [])

        await sync_to_async(schema.refresh_from_db)()
        assert schema.sync_type_config.get("reset_pipeline", None) is None

    return workflow_id, inputs


async def _execute_run(workflow_id: str, inputs: ExternalDataWorkflowInputs, mock_data_response):
    def mock_paginate(
        class_self,
        path: str = "",
        method: Any = "GET",
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        auth: Optional[Any] = None,
        paginator: Optional[Any] = None,
        data_selector: Optional[Any] = None,
        hooks: Optional[Any] = None,
    ):
        return iter(mock_data_response)

    def mock_to_session_credentials(class_self):
        return {
            "aws_access_key_id": settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            "aws_secret_access_key": settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            "endpoint_url": settings.OBJECT_STORAGE_ENDPOINT,
            "aws_session_token": None,
            "AWS_ALLOW_HTTP": "true",
            "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
        }

    def mock_to_object_store_rs_credentials(class_self):
        return {
            "aws_access_key_id": settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            "aws_secret_access_key": settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            "endpoint_url": settings.OBJECT_STORAGE_ENDPOINT,
            "region": "us-east-1",
            "AWS_ALLOW_HTTP": "true",
            "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
        }

    with (
        mock.patch.object(RESTClient, "paginate", mock_paginate),
        override_settings(
            BUCKET_URL=f"s3://{BUCKET_NAME}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        mock.patch.object(AwsCredentials, "to_session_credentials", mock_to_session_credentials),
        mock.patch.object(AwsCredentials, "to_object_store_rs_credentials", mock_to_object_store_rs_credentials),
    ):
        async with await WorkflowEnvironment.start_time_skipping() as activity_environment:
            async with Worker(
                activity_environment.client,
                task_queue=DATA_WAREHOUSE_TASK_QUEUE,
                workflows=[ExternalDataJobWorkflow],
                activities=ACTIVITIES,  # type: ignore
                workflow_runner=UnsandboxedWorkflowRunner(),
                activity_executor=ThreadPoolExecutor(max_workers=50),
                max_concurrent_activities=50,
            ):
                await activity_environment.client.execute_workflow(
                    ExternalDataJobWorkflow.run,
                    inputs,
                    id=workflow_id,
                    task_queue=DATA_WAREHOUSE_TASK_QUEUE,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_balance_transactions(team, stripe_balance_transaction):
    await _run(
        team=team,
        schema_name=STRIPE_BALANCE_TRANSACTION_RESOURCE_NAME,
        table_name="stripe_balancetransaction",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_balance_transaction["data"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_charges(team, stripe_charge):
    await _run(
        team=team,
        schema_name=STRIPE_CHARGE_RESOURCE_NAME,
        table_name="stripe_charge",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_charge["data"],
    )

    # Get team from the DB to remove cached config value
    team = await sync_to_async(Team.objects.get)(id=team.id)
    assert team.revenue_analytics_config.notified_first_sync


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_customer(team, stripe_customer):
    await _run(
        team=team,
        schema_name=STRIPE_CUSTOMER_RESOURCE_NAME,
        table_name="stripe_customer",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_customer["data"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_invoice(team, stripe_invoice):
    await _run(
        team=team,
        schema_name=STRIPE_INVOICE_RESOURCE_NAME,
        table_name="stripe_invoice",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_invoice["data"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_price(team, stripe_price):
    await _run(
        team=team,
        schema_name=STRIPE_PRICE_RESOURCE_NAME,
        table_name="stripe_price",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_price["data"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_product(team, stripe_product):
    await _run(
        team=team,
        schema_name=STRIPE_PRODUCT_RESOURCE_NAME,
        table_name="stripe_product",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_product["data"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stripe_subscription(team, stripe_subscription):
    await _run(
        team=team,
        schema_name=STRIPE_SUBSCRIPTION_RESOURCE_NAME,
        table_name="stripe_subscription",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_subscription["data"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_brands(team, zendesk_brands):
    await _run(
        team=team,
        schema_name="brands",
        table_name="zendesk_brands",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_brands["brands"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_organizations(team, zendesk_organizations):
    await _run(
        team=team,
        schema_name="organizations",
        table_name="zendesk_organizations",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_organizations["organizations"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_groups(team, zendesk_groups):
    await _run(
        team=team,
        schema_name="groups",
        table_name="zendesk_groups",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_groups["groups"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_sla_policies(team, zendesk_sla_policies):
    await _run(
        team=team,
        schema_name="sla_policies",
        table_name="zendesk_sla_policies",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_sla_policies["sla_policies"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_users(team, zendesk_users):
    await _run(
        team=team,
        schema_name="users",
        table_name="zendesk_users",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_users["users"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_ticket_fields(team, zendesk_ticket_fields):
    await _run(
        team=team,
        schema_name="ticket_fields",
        table_name="zendesk_ticket_fields",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_ticket_fields["ticket_fields"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_ticket_events(team, zendesk_ticket_events):
    await _run(
        team=team,
        schema_name="ticket_events",
        table_name="zendesk_ticket_events",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_ticket_events["ticket_events"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_tickets(team, zendesk_tickets):
    await _run(
        team=team,
        schema_name="tickets",
        table_name="zendesk_tickets",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_tickets["tickets"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_zendesk_ticket_metric_events(team, zendesk_ticket_metric_events):
    await _run(
        team=team,
        schema_name="ticket_metric_events",
        table_name="zendesk_ticket_metric_events",
        source_type="Zendesk",
        job_inputs={
            "zendesk_subdomain": "test",
            "zendesk_api_key": "test_api_key",
            "zendesk_email_address": "test@posthog.com",
        },
        mock_data_response=zendesk_ticket_metric_events["ticket_metric_events"],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_chargebee_customer(team, chargebee_customer):
    await _run(
        team=team,
        schema_name="Customers",
        table_name="chargebee_customers",
        source_type="Chargebee",
        job_inputs={"api_key": "test-key", "site_name": "site-test"},
        mock_data_response=[chargebee_customer["list"][0]["customer"]],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_reset_pipeline(team, stripe_balance_transaction):
    await _run(
        team=team,
        schema_name="BalanceTransaction",
        table_name="stripe_balancetransaction",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_balance_transaction["data"],
        sync_type_config={"reset_pipeline": True},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_postgres_binary_columns(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.binary_col_test (id integer, binary_column bytea)".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.binary_col_test (id, binary_column) VALUES (1, '\x48656C6C6F')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    await _run(
        team=team,
        schema_name="binary_col_test",
        table_name="postgres_binary_col_test",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
    )

    res = await sync_to_async(execute_hogql_query)(f"SELECT * FROM postgres_binary_col_test", team)
    columns = res.columns

    assert columns is not None
    assert len(columns) == 1
    assert any(x == "id" for x in columns)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delta_wrapper_files(team, stripe_balance_transaction, minio_client):
    workflow_id, inputs = await _run(
        team=team,
        schema_name="BalanceTransaction",
        table_name="stripe_balancetransaction",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_balance_transaction["data"],
    )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(
            team_id=team.pk,
            pipeline_id=inputs.external_data_source_id,
        ).order_by("-created_at")

        return list(jobs)

    jobs = await get_jobs()
    latest_job = jobs[0]
    folder_path = await sync_to_async(latest_job.folder_path)()

    s3_objects = await minio_client.list_objects_v2(
        Bucket=BUCKET_NAME, Prefix=f"{folder_path}/balance_transaction__query/"
    )

    assert len(s3_objects["Contents"]) != 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_funnels_lazy_joins_ordering(team, stripe_customer):
    # Tests that funnels work in PERSON_ID_OVERRIDE_PROPERTIES_JOINED PoE mode when using extended person properties
    await _run(
        team=team,
        schema_name="Customer",
        table_name="stripe_customer",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_customer["data"],
    )

    await sync_to_async(DataWarehouseJoin.objects.create)(
        team=team,
        source_table_name="persons",
        source_table_key="properties.email",
        joining_table_name="stripe_customer",
        joining_table_key="email",
        field_name="stripe_customer",
    )

    query = FunnelsQuery(
        series=[EventsNode(), EventsNode()],
        breakdownFilter=BreakdownFilter(
            breakdown_type=BreakdownType.DATA_WAREHOUSE_PERSON_PROPERTY, breakdown="stripe_customer.email"
        ),
    )
    funnel_class = Funnel(context=FunnelQueryContext(query=query, team=team))

    query_ast = funnel_class.get_query()
    await sync_to_async(execute_hogql_query)(
        query_type="FunnelsQuery",
        query=query_ast,
        team=team,
        modifiers=create_default_modifiers_for_team(
            team, HogQLQueryModifiers(personsOnEventsMode=PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_JOINED)
        ),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_postgres_schema_evolution(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_table (id integer)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (1)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    _workflow_id, inputs = await _run(
        team=team,
        schema_name="test_table",
        table_name="postgres_test_table",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
    )

    res = await sync_to_async(execute_hogql_query)("SELECT * FROM postgres_test_table", team)
    columns = res.columns

    assert columns is not None
    assert len(columns) == 1
    assert any(x == "id" for x in columns)

    # Evole schema
    await postgres_connection.execute(
        "ALTER TABLE {schema}.test_table ADD new_col integer".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id, new_col) VALUES (2, 2)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    # Execute the same schema again - load
    await _execute_run(str(uuid.uuid4()), inputs, [])

    res = await sync_to_async(execute_hogql_query)("SELECT * FROM postgres_test_table", team)
    columns = res.columns

    assert columns is not None
    assert len(columns) == 2
    assert any(x == "id" for x in columns)
    assert any(x == "new_col" for x in columns)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_sql_database_missing_incremental_values(team, postgres_config, postgres_connection):
    await postgres_connection.execute("CREATE SCHEMA IF NOT EXISTS {schema}".format(schema=postgres_config["schema"]))
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_table (id integer)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (1)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (null)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    await _run(
        team=team,
        schema_name="test_table",
        table_name="postgres_test_table",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
    )

    res = await sync_to_async(execute_hogql_query)("SELECT * FROM postgres_test_table", team)
    columns = res.columns

    assert columns is not None
    assert len(columns) == 1
    assert any(x == "id" for x in columns)

    # Exclude rows that don't have the incremental cursor key set
    assert len(res.results) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_sql_database_incremental_initial_value(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_table (id integer)".format(schema=postgres_config["schema"])
    )
    # Setting `id` to `0` - the same as an `integer` incremental initial value
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (0)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    await _run(
        team=team,
        schema_name="test_table",
        table_name="postgres_test_table",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
    )

    res = await sync_to_async(execute_hogql_query)("SELECT * FROM postgres_test_table", team)
    columns = res.columns

    assert columns is not None
    assert len(columns) == 1
    assert any(x == "id" for x in columns)

    # Include rows that have the same incremental value as the `initial_value`
    assert len(res.results) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_billing_limits(team, stripe_customer):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name="Customer",
        team_id=team.pk,
        source_id=source.pk,
        sync_type=ExternalDataSchema.SyncType.FULL_REFRESH,
        sync_type_config={},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
    )

    with mock.patch(
        "posthog.temporal.data_imports.workflow_activities.check_billing_limits.list_limited_team_attributes",
    ) as mock_list_limited_team_attributes:
        mock_list_limited_team_attributes.return_value = [team.api_token]

        await _execute_run(workflow_id, inputs, stripe_customer["data"])

    job: ExternalDataJob = await sync_to_async(ExternalDataJob.objects.get)(team_id=team.id, schema_id=schema.pk)

    assert job.status == ExternalDataJob.Status.CANCELLED

    with pytest.raises(Exception):
        await sync_to_async(execute_hogql_query)("SELECT * FROM stripe_customer", team)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_external_job_failure(team, stripe_customer):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name="Customer",
        team_id=team.pk,
        source_id=source.pk,
        sync_type=ExternalDataSchema.SyncType.FULL_REFRESH,
        sync_type_config={},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
    )

    with mock.patch(
        "posthog.temporal.data_imports.workflow_activities.check_billing_limits.list_limited_team_attributes",
    ) as mock_list_limited_team_attributes:
        mock_list_limited_team_attributes.side_effect = Exception("Ruhoh!")

        with pytest.raises(Exception):
            await _execute_run(workflow_id, inputs, stripe_customer["data"])

    job: ExternalDataJob = await sync_to_async(ExternalDataJob.objects.get)(team_id=team.id, schema_id=schema.pk)

    assert job.status == ExternalDataJob.Status.FAILED

    with pytest.raises(Exception):
        await sync_to_async(execute_hogql_query)("SELECT * FROM stripe_customer", team)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_external_job_failure_no_job_model(team, stripe_customer):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name="Customer",
        team_id=team.pk,
        source_id=source.pk,
        sync_type=ExternalDataSchema.SyncType.FULL_REFRESH,
        sync_type_config={},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
    )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(team_id=team.id, schema_id=schema.pk)

        return list(jobs)

    with mock.patch.object(
        ExternalDataJob.objects,
        "create",
    ) as create_external_data_job:
        create_external_data_job.side_effect = Exception("Ruhoh!")

        with pytest.raises(Exception):
            await _execute_run(workflow_id, inputs, stripe_customer["data"])

    jobs: list[ExternalDataJob] = await get_jobs()

    assert len(jobs) == 0

    with pytest.raises(Exception):
        await sync_to_async(execute_hogql_query)("SELECT * FROM stripe_customer", team)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_non_retryable_error(team, stripe_customer):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name="Customer",
        team_id=team.pk,
        source_id=source.pk,
        sync_type=ExternalDataSchema.SyncType.FULL_REFRESH,
        sync_type_config={},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
    )

    with (
        mock.patch(
            "posthog.temporal.data_imports.workflow_activities.check_billing_limits.list_limited_team_attributes",
        ) as mock_list_limited_team_attributes,
        mock.patch.object(posthoganalytics, "capture") as capture_mock,
    ):
        mock_list_limited_team_attributes.side_effect = Exception(
            "401 Client Error: Unauthorized for url: https://api.stripe.com"
        )

        with pytest.raises(Exception):
            await _execute_run(workflow_id, inputs, stripe_customer["data"])

        capture_mock.assert_called_once()

    job: ExternalDataJob = await sync_to_async(ExternalDataJob.objects.get)(team_id=team.id, schema_id=schema.pk)
    await sync_to_async(schema.refresh_from_db)()

    assert job.status == ExternalDataJob.Status.FAILED
    assert schema.should_sync is False

    with pytest.raises(Exception):
        await sync_to_async(execute_hogql_query)("SELECT * FROM stripe_customer", team)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_non_retryable_error_with_special_characters(team, stripe_customer):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name="Customer",
        team_id=team.pk,
        source_id=source.pk,
        sync_type=ExternalDataSchema.SyncType.FULL_REFRESH,
        sync_type_config={},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
    )

    with (
        mock.patch(
            "posthog.temporal.data_imports.workflow_activities.check_billing_limits.list_limited_team_attributes",
        ) as mock_list_limited_team_attributes,
        mock.patch.object(posthoganalytics, "capture") as capture_mock,
    ):
        mock_list_limited_team_attributes.side_effect = Exception(
            "401 Client Error:\nUnauthorized for url: https://api.stripe.com"
        )

        with pytest.raises(Exception):
            await _execute_run(workflow_id, inputs, stripe_customer["data"])

        capture_mock.assert_called_once()

    job: ExternalDataJob = await sync_to_async(ExternalDataJob.objects.get)(team_id=team.id, schema_id=schema.pk)
    await sync_to_async(schema.refresh_from_db)()

    assert job.status == ExternalDataJob.Status.FAILED
    assert schema.should_sync is False

    with pytest.raises(Exception):
        await sync_to_async(execute_hogql_query)("SELECT * FROM stripe_customer", team)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inconsistent_types_in_data(team):
    source = await sync_to_async(ExternalDataSource.objects.create)(
        source_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        team=team,
        status="running",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
    )

    schema = await sync_to_async(ExternalDataSchema.objects.create)(
        name="Price",
        team_id=team.pk,
        source_id=source.pk,
        sync_type=ExternalDataSchema.SyncType.FULL_REFRESH,
        sync_type_config={},
    )

    workflow_id = str(uuid.uuid4())
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=source.pk,
        external_data_schema_id=schema.id,
    )

    await _execute_run(
        workflow_id,
        inputs,
        [
            {"id": "txn_1MiN3gLkdIwHu7ixxapQrznl", "type": "transfer"},
            {"id": "txn_1MiN3gLkdIwHu7ixxapQrznl", "type": ["transfer", "another_value"]},
        ],
    )

    res = await sync_to_async(execute_hogql_query)(f"SELECT * FROM stripe_price", team)
    columns = res.columns
    results = res.results

    assert columns is not None
    assert any(x == "id" for x in columns)
    assert any(x == "type" for x in columns)

    assert results is not None
    assert len(results) == 2

    id_index = columns.index("id")
    arr_index = columns.index("type")

    assert results[0][id_index] == "txn_1MiN3gLkdIwHu7ixxapQrznl"
    assert results[0][arr_index] == '["transfer"]'

    assert results[1][id_index] == "txn_1MiN3gLkdIwHu7ixxapQrznl"
    assert results[1][arr_index] == '["transfer","another_value"]'


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_postgres_uuid_type(team, postgres_config, postgres_connection):
    await _run(
        team=team,
        schema_name="BalanceTransaction",
        table_name="stripe_balancetransaction",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=[{"id": uuid.uuid4()}],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_decimal_down_scales(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.downsizing_column (id integer, dec_col numeric(10, 2))".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.downsizing_column (id, dec_col) VALUES (1, 12345.60)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.commit()

    workflow_id, inputs = await _run(
        team=team,
        schema_name="downsizing_column",
        table_name="postgres_downsizing_column",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
    )

    await postgres_connection.execute(
        "ALTER TABLE {schema}.downsizing_column ALTER COLUMN dec_col type numeric(9, 2) using dec_col::numeric(9, 2);".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.downsizing_column (id, dec_col) VALUES (1, 1234567.89)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.commit()

    await _execute_run(str(uuid.uuid4()), inputs, [])


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_missing_source(team, stripe_balance_transaction):
    inputs = ExternalDataWorkflowInputs(
        team_id=team.id,
        external_data_source_id=uuid.uuid4(),
        external_data_schema_id=uuid.uuid4(),
    )

    with (
        pytest.raises(Exception) as e,
        mock.patch(
            "posthog.temporal.data_imports.workflow_activities.create_job_model.delete_external_data_schedule"
        ) as mock_delete_external_data_schedule,
    ):
        await _execute_run(str(uuid.uuid4()), inputs, [])

    exc = cast(Any, e)

    assert exc.value is not None
    assert exc.value.cause is not None
    assert exc.value.cause.cause is not None
    assert exc.value.cause.cause.message is not None

    assert exc.value.cause.cause.message == "Source or schema no longer exists - deleted temporal schedule"

    mock_delete_external_data_schedule.assert_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_postgres_nan_numerical_values(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.numerical_nan (id integer, nan_column numeric)".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.numerical_nan (id, nan_column) VALUES (1, 'NaN'::numeric)".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    await _run(
        team=team,
        schema_name="numerical_nan",
        table_name="postgres_numerical_nan",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
    )

    res = await sync_to_async(execute_hogql_query)(f"SELECT * FROM postgres_numerical_nan", team)
    columns = res.columns
    results = res.results

    assert columns is not None
    assert len(columns) == 2
    assert any(x == "id" for x in columns)
    assert any(x == "nan_column" for x in columns)

    assert results is not None
    assert len(results) == 1

    id_index = columns.index("id")
    nan_index = columns.index("nan_column")

    assert results[0][id_index] == 1
    assert results[0][nan_index] is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delete_table_on_reset(team, stripe_balance_transaction):
    with (
        mock.patch.object(s3fs.S3FileSystem, "delete") as mock_s3_delete,
    ):
        workflow_id, inputs = await _run(
            team=team,
            schema_name="BalanceTransaction",
            table_name="stripe_balancetransaction",
            source_type="Stripe",
            job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
            mock_data_response=stripe_balance_transaction["data"],
            sync_type_config={"reset_pipeline": True},
        )

        schema = await sync_to_async(ExternalDataSchema.objects.get)(id=inputs.external_data_schema_id)

        assert schema.sync_type_config is not None and isinstance(schema.sync_type_config, dict)
        schema.sync_type_config["reset_pipeline"] = True

        await sync_to_async(schema.save)()

        await _execute_run(str(uuid.uuid4()), inputs, stripe_balance_transaction["data"])

    mock_s3_delete.assert_called()

    await sync_to_async(schema.refresh_from_db)()

    assert schema.sync_type_config is not None and isinstance(schema.sync_type_config, dict)
    assert "reset_pipeline" not in schema.sync_type_config.keys()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_billable_job(team, stripe_balance_transaction):
    workflow_id, inputs = await _run(
        team=team,
        schema_name="BalanceTransaction",
        table_name="stripe_balancetransaction",
        source_type="Stripe",
        job_inputs={"stripe_secret_key": "test-key", "stripe_account_id": "acct_id"},
        mock_data_response=stripe_balance_transaction["data"],
        billable=False,
    )

    run: ExternalDataJob = await get_latest_run_if_exists(team_id=team.pk, pipeline_id=inputs.external_data_source_id)
    assert run.billable is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delta_no_merging_on_first_sync(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_table (id integer)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (1)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (2)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    with (
        mock.patch("posthog.temporal.data_imports.pipelines.postgres.postgres.DEFAULT_CHUNK_SIZE", 1),
        mock.patch.object(DeltaTable, "merge") as mock_merge,
        mock.patch.object(deltalake, "write_deltalake") as mock_write,
        mock.patch.object(PipelineNonDLT, "_post_run_operations") as mock_post_run_operations,
    ):
        await _run(
            team=team,
            schema_name="test_table",
            table_name="postgres_test_table",
            source_type="Postgres",
            job_inputs={
                "host": postgres_config["host"],
                "port": postgres_config["port"],
                "database": postgres_config["database"],
                "user": postgres_config["user"],
                "password": postgres_config["password"],
                "schema": postgres_config["schema"],
                "ssh_tunnel_enabled": "False",
            },
            mock_data_response=[],
            sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
            sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
            ignore_assertions=True,
        )

    mock_post_run_operations.assert_called_once()

    mock_merge.assert_not_called()
    assert mock_write.call_count == 2

    first_call_args, first_call_kwargs = mock_write.call_args_list[0]
    second_call_args, second_call_kwargs = mock_write.call_args_list[1]

    # The first call should be an append
    assert first_call_kwargs == {
        "mode": "overwrite",
        "schema_mode": "overwrite",
        "table_or_uri": mock.ANY,
        "data": mock.ANY,
        "partition_by": mock.ANY,
        "engine": "rust",
    }

    # The last call should be an append
    assert second_call_kwargs == {
        "mode": "append",
        "schema_mode": "merge",
        "table_or_uri": mock.ANY,
        "data": mock.ANY,
        "partition_by": mock.ANY,
        "engine": "rust",
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delta_no_merging_on_first_sync_after_reset(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_table (id integer)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (1)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_table (id) VALUES (2)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    workflow_id, inputs = await _run(
        team=team,
        schema_name="test_table",
        table_name="postgres_test_table",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
        ignore_assertions=True,
    )

    with (
        mock.patch("posthog.temporal.data_imports.pipelines.postgres.postgres.DEFAULT_CHUNK_SIZE", 1),
        mock.patch.object(DeltaTable, "merge") as mock_merge,
        mock.patch.object(deltalake, "write_deltalake") as mock_write,
        mock.patch.object(PipelineNonDLT, "_post_run_operations") as mock_post_run_operations,
    ):
        await _execute_run(
            str(uuid.uuid4()),
            ExternalDataWorkflowInputs(
                team_id=inputs.team_id,
                external_data_source_id=inputs.external_data_source_id,
                external_data_schema_id=inputs.external_data_schema_id,
                reset_pipeline=True,
            ),
            [],
        )

    mock_post_run_operations.assert_called_once()

    mock_merge.assert_not_called()
    assert mock_write.call_count == 2

    first_call_args, first_call_kwargs = mock_write.call_args_list[0]
    second_call_args, second_call_kwargs = mock_write.call_args_list[1]

    # The first call should be an overwrite
    assert first_call_kwargs == {
        "mode": "overwrite",
        "schema_mode": "overwrite",
        "table_or_uri": mock.ANY,
        "data": mock.ANY,
        "partition_by": mock.ANY,
        "engine": "rust",
    }

    # The subsequent call should be an append
    assert second_call_kwargs == {
        "mode": "append",
        "schema_mode": "merge",
        "table_or_uri": mock.ANY,
        "data": mock.ANY,
        "partition_by": mock.ANY,
        "engine": "rust",
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_partition_folders_with_int_id(team, postgres_config, postgres_connection, minio_client):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_partition_folders (id integer, created_at timestamp)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (1, '2025-01-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (2, '2025-02-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    workflow_id, inputs = await _run(
        team=team,
        schema_name="test_partition_folders",
        table_name="postgres_test_partition_folders",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
        ignore_assertions=True,
    )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(
            team_id=team.pk,
            pipeline_id=inputs.external_data_source_id,
        ).order_by("-created_at")

        return list(jobs)

    jobs = await get_jobs()
    latest_job = jobs[0]
    folder_path = await sync_to_async(latest_job.folder_path)()

    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")

    # Using numerical primary key causes partitions not be md5'd
    assert any(f"{PARTITION_KEY}=0" in obj["Key"] for obj in s3_objects["Contents"])

    schema = await ExternalDataSchema.objects.aget(id=inputs.external_data_schema_id)
    assert schema.partitioning_enabled is True
    assert schema.partitioning_keys == ["id"]
    assert schema.partition_mode == "numerical"
    assert schema.partition_count is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_partition_folders_with_uuid_id_and_created_at(team, postgres_config, postgres_connection, minio_client):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_partition_folders (id uuid, created_at timestamp)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES ('{uuid}', '2025-01-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"], uuid=str(uuid.uuid4())
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES ('{uuid}', '2025-02-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"], uuid=str(uuid.uuid4())
        )
    )
    await postgres_connection.commit()

    workflow_id, inputs = await _run(
        team=team,
        schema_name="test_partition_folders",
        table_name="postgres_test_partition_folders",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "created_at", "incremental_field_type": "timestamp"},
        ignore_assertions=True,
    )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(
            team_id=team.pk,
            pipeline_id=inputs.external_data_source_id,
        ).order_by("-created_at")

        return list(jobs)

    jobs = await get_jobs()
    latest_job = jobs[0]
    folder_path = await sync_to_async(latest_job.folder_path)()

    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")

    # Using datetime partition mode with created_at
    assert any(f"{PARTITION_KEY}=2025-01" in obj["Key"] for obj in s3_objects["Contents"])
    assert any(f"{PARTITION_KEY}=2025-02" in obj["Key"] for obj in s3_objects["Contents"])

    schema = await ExternalDataSchema.objects.aget(id=inputs.external_data_schema_id)
    assert schema.partitioning_enabled is True
    assert schema.partitioning_keys == ["created_at"]
    assert schema.partition_mode == "datetime"
    assert schema.partition_count is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_partition_folders_with_uuid_id_and_created_at_with_day_format(
    team, postgres_config, postgres_connection, minio_client
):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_partition_folders (id uuid, created_at timestamp)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES ('{uuid}', '2025-01-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"], uuid=str(uuid.uuid4())
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES ('{uuid}', '2025-01-02T12:00:00.000Z')".format(
            schema=postgres_config["schema"], uuid=str(uuid.uuid4())
        )
    )
    await postgres_connection.commit()

    workflow_id, inputs = await _run(
        team=team,
        schema_name="test_partition_folders",
        table_name="postgres_test_partition_folders",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "created_at", "incremental_field_type": "timestamp"},
        ignore_assertions=True,
    )

    # Set the parition format on the schema - this will persist after a reset_pipeline
    schema: ExternalDataSchema = await sync_to_async(ExternalDataSchema.objects.get)(id=inputs.external_data_schema_id)
    schema.sync_type_config["partition_format"] = "day"
    await sync_to_async(schema.save)()

    # Resync with reset_pipeline = True
    await _execute_run(
        str(uuid.uuid4()),
        ExternalDataWorkflowInputs(
            team_id=inputs.team_id,
            external_data_source_id=inputs.external_data_source_id,
            external_data_schema_id=inputs.external_data_schema_id,
            reset_pipeline=True,
        ),
        [],
    )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(
            team_id=team.pk,
            pipeline_id=inputs.external_data_source_id,
        ).order_by("-created_at")

        return list(jobs)

    jobs = await get_jobs()
    latest_job = jobs[0]
    folder_path = await sync_to_async(latest_job.folder_path)()

    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")

    # Using datetime partition mode with created_at - formatted to the day
    assert any(f"{PARTITION_KEY}=2025-01-01" in obj["Key"] for obj in s3_objects["Contents"])
    assert any(f"{PARTITION_KEY}=2025-01-02" in obj["Key"] for obj in s3_objects["Contents"])

    schema = await ExternalDataSchema.objects.aget(id=inputs.external_data_schema_id)
    assert schema.partitioning_enabled is True
    assert schema.partitioning_keys == ["created_at"]
    assert schema.partition_mode == "datetime"
    assert schema.partition_format == "day"
    assert schema.partition_count is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_partition_folders_with_existing_table(team, postgres_config, postgres_connection, minio_client):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_partition_folders (id integer, created_at timestamp)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (1, '2025-01-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (2, '2025-02-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    # Emulate an existing table with no partitions
    with mock.patch(
        "posthog.temporal.data_imports.pipelines.pipeline.pipeline.should_partition_table", return_value=False
    ):
        workflow_id, inputs = await _run(
            team=team,
            schema_name="test_partition_folders",
            table_name="postgres_test_partition_folders",
            source_type="Postgres",
            job_inputs={
                "host": postgres_config["host"],
                "port": postgres_config["port"],
                "database": postgres_config["database"],
                "user": postgres_config["user"],
                "password": postgres_config["password"],
                "schema": postgres_config["schema"],
                "ssh_tunnel_enabled": "False",
            },
            mock_data_response=[],
            sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
            sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
            ignore_assertions=True,
        )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(
            team_id=team.pk,
            pipeline_id=inputs.external_data_source_id,
        ).order_by("-created_at")

        return list(jobs)

    jobs = await get_jobs()
    latest_job = jobs[0]
    folder_path = await sync_to_async(latest_job.folder_path)()

    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")

    # Confirm there are no partitions in S3
    assert not any(PARTITION_KEY in obj["Key"] for obj in s3_objects["Contents"])

    # Add new data to the postgres table
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (3, '2025-03-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    # Resync
    await _execute_run(str(uuid.uuid4()), inputs, [])

    # Reconfirm there are no partitions
    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")
    assert not any(PARTITION_KEY in obj["Key"] for obj in s3_objects["Contents"])

    schema = await ExternalDataSchema.objects.aget(id=inputs.external_data_schema_id)
    assert schema.partitioning_enabled is False
    assert schema.partitioning_keys is None
    assert schema.partition_count is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_partition_folders_with_existing_table_and_pipeline_reset(
    team, postgres_config, postgres_connection, minio_client
):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_partition_folders (id integer, created_at timestamp)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (1, '2025-01-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (2, '2025-02-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    # Emulate an existing table with no partitions
    with mock.patch(
        "posthog.temporal.data_imports.pipelines.pipeline.pipeline.should_partition_table", return_value=False
    ):
        workflow_id, inputs = await _run(
            team=team,
            schema_name="test_partition_folders",
            table_name="postgres_test_partition_folders",
            source_type="Postgres",
            job_inputs={
                "host": postgres_config["host"],
                "port": postgres_config["port"],
                "database": postgres_config["database"],
                "user": postgres_config["user"],
                "password": postgres_config["password"],
                "schema": postgres_config["schema"],
                "ssh_tunnel_enabled": "False",
            },
            mock_data_response=[],
            sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
            sync_type_config={"incremental_field": "id", "incremental_field_type": "integer"},
            ignore_assertions=True,
        )

    @sync_to_async
    def get_jobs():
        jobs = ExternalDataJob.objects.filter(
            team_id=team.pk,
            pipeline_id=inputs.external_data_source_id,
        ).order_by("-created_at")

        return list(jobs)

    jobs = await get_jobs()
    latest_job = jobs[0]
    folder_path = await sync_to_async(latest_job.folder_path)()

    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")

    # Confirm there are no partitions in S3
    assert not any(PARTITION_KEY in obj["Key"] for obj in s3_objects["Contents"])

    # Update the schema to be incremental based on the created_at field
    schema: ExternalDataSchema = await sync_to_async(ExternalDataSchema.objects.get)(id=inputs.external_data_schema_id)
    schema.sync_type_config = {
        "incremental_field": "created_at",
        "incremental_field_type": "timestamp",
        "incremental_field_last_value": "2025-02-01T12:00:00.000Z",
    }
    await sync_to_async(schema.save)()

    # Resync with reset_pipeline = True
    await _execute_run(
        str(uuid.uuid4()),
        ExternalDataWorkflowInputs(
            team_id=inputs.team_id,
            external_data_source_id=inputs.external_data_source_id,
            external_data_schema_id=inputs.external_data_schema_id,
            reset_pipeline=True,
        ),
        [],
    )

    # Confirm the table now has partitions
    s3_objects = await minio_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{folder_path}/test_partition_folders/")

    assert any(f"{PARTITION_KEY}=" in obj["Key"] for obj in s3_objects["Contents"])

    schema = await ExternalDataSchema.objects.aget(id=inputs.external_data_schema_id)
    assert schema.partitioning_enabled is True
    assert schema.partitioning_keys == ["id"]
    assert schema.partition_count is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_partition_folders_delta_merge_called_with_partition_predicate(
    team, postgres_config, postgres_connection
):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.test_partition_folders (id integer, created_at timestamp)".format(
            schema=postgres_config["schema"]
        )
    )

    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (1, '2025-01-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.test_partition_folders (id, created_at) VALUES (2, '2025-02-01T12:00:00.000Z')".format(
            schema=postgres_config["schema"]
        )
    )
    await postgres_connection.commit()

    # Emulate an existing table with no partitions
    workflow_id, inputs = await _run(
        team=team,
        schema_name="test_partition_folders",
        table_name="postgres_test_partition_folders",
        source_type="Postgres",
        job_inputs={
            "host": postgres_config["host"],
            "port": postgres_config["port"],
            "database": postgres_config["database"],
            "user": postgres_config["user"],
            "password": postgres_config["password"],
            "schema": postgres_config["schema"],
            "ssh_tunnel_enabled": "False",
        },
        mock_data_response=[],
        sync_type=ExternalDataSchema.SyncType.INCREMENTAL,
        sync_type_config={"incremental_field": "created_at", "incremental_field_type": "timestamp"},
        ignore_assertions=True,
    )

    with (
        mock.patch("posthog.temporal.data_imports.pipelines.postgres.postgres.DEFAULT_CHUNK_SIZE", 1),
        mock.patch.object(DeltaTable, "merge") as mock_merge,
        mock.patch.object(deltalake, "write_deltalake") as mock_write,
        mock.patch.object(PipelineNonDLT, "_post_run_operations") as mock_post_run_operations,
    ):
        # Mocking the return of the delta merge as it gets JSON'ified
        mock_merge_instance = mock_merge.return_value
        mock_when_matched = mock_merge_instance.when_matched_update_all.return_value
        mock_when_not_matched = mock_when_matched.when_not_matched_insert_all.return_value
        mock_when_not_matched.execute.return_value = {}

        await _execute_run(
            str(uuid.uuid4()),
            inputs,
            [],
        )

    mock_post_run_operations.assert_called_once()

    mock_write.assert_not_called()
    assert mock_merge.call_count == 1

    merge_call_args, first_call_kwargs = mock_merge.call_args_list[0]

    assert first_call_kwargs == {
        "source": mock.ANY,
        "source_alias": "source",
        "target_alias": "target",
        "predicate": f"source.id = target.id AND source.{PARTITION_KEY} = target.{PARTITION_KEY} AND target.{PARTITION_KEY} = '0'",
        "streamed_exec": True,
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_row_tracking_incrementing(team, postgres_config, postgres_connection):
    await postgres_connection.execute(
        "CREATE TABLE IF NOT EXISTS {schema}.row_tracking (id integer)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.execute(
        "INSERT INTO {schema}.row_tracking (id) VALUES (1)".format(schema=postgres_config["schema"])
    )
    await postgres_connection.commit()

    with (
        mock.patch("posthog.temporal.data_imports.pipelines.pipeline.pipeline.decrement_rows") as mock_decrement_rows,
        mock.patch("posthog.temporal.data_imports.external_data_job.finish_row_tracking") as mock_finish_row_tracking,
    ):
        _, inputs = await _run(
            team=team,
            schema_name="row_tracking",
            table_name="postgres_row_tracking",
            source_type="Postgres",
            job_inputs={
                "host": postgres_config["host"],
                "port": postgres_config["port"],
                "database": postgres_config["database"],
                "user": postgres_config["user"],
                "password": postgres_config["password"],
                "schema": postgres_config["schema"],
                "ssh_tunnel_enabled": "False",
            },
            mock_data_response=[],
        )

    schema_id = inputs.external_data_schema_id

    mock_decrement_rows.assert_called_once_with(team.id, schema_id, 1)
    mock_finish_row_tracking.assert_called_once()

    assert schema_id is not None
    row_count_in_redis = get_rows(team.id, schema_id)

    assert row_count_in_redis == 1

    res = await sync_to_async(execute_hogql_query)(f"SELECT * FROM postgres_row_tracking", team)
    columns = res.columns

    assert columns is not None
    assert len(columns) == 1
    assert any(x == "id" for x in columns)

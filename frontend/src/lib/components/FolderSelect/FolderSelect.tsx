import { IconCheckCircle } from '@posthog/icons'
import clsx from 'clsx'
import { useActions, useValues } from 'kea'
import { LemonInput } from 'lib/lemon-ui/LemonInput'
import { LemonTree, LemonTreeRef, TreeDataItem } from 'lib/lemon-ui/LemonTree/LemonTree'
import { ButtonPrimitive } from 'lib/ui/Button/ButtonPrimitives'
import { ContextMenuGroup, ContextMenuItem } from 'lib/ui/ContextMenu/ContextMenu'
import { DropdownMenuGroup, DropdownMenuItem } from 'lib/ui/DropdownMenu/DropdownMenu'
import { ReactNode, useEffect, useRef, useState } from 'react'

import { projectTreeLogic, ProjectTreeLogicProps } from '~/layout/panel-layout/ProjectTree/projectTreeLogic'
import { FileSystemEntry } from '~/queries/schema/schema-general'

export interface FolderSelectProps {
    /** The folder to select */
    value?: string
    /** Callback when a folder is selected */
    onChange?: (folder: string) => void
    /** Class name for the component */
    className?: string
    /** Root for folder */
    root?: string
    /** Include "products://" in the final path */
    includeProtocol?: boolean
}

/** Input component for selecting a folder */
let counter = 0

export function FolderSelect({ value, onChange, root, className, includeProtocol }: FolderSelectProps): JSX.Element {
    const [key] = useState(() => `folder-select-${counter++}`)
    const props: ProjectTreeLogicProps = { key, defaultOnlyFolders: true, root }

    const { searchTerm, expandedSearchFolders, expandedFolders, fullFileSystemFiltered, treeTableKeys, editingItemId } =
        useValues(projectTreeLogic(props))
    const {
        setSearchTerm,
        setExpandedSearchFolders,
        setExpandedFolders,
        createFolder,
        expandProjectFolder,
        setEditingItemId,
        rename,
        toggleFolderOpen,
    } = useActions(projectTreeLogic(props))
    const treeRef = useRef<LemonTreeRef>(null)
    const [selectedFolder, setSelectedFolder] = useState<string | undefined>(value)

    useEffect(() => {
        if (includeProtocol) {
            if (value?.startsWith('project://')) {
                expandProjectFolder(value.replace('project://', ''))
            }
        } else {
            expandProjectFolder(value || '')
        }
    }, [value])

    function getItemContextMenu(type: 'context' | 'dropdown'): (item: TreeDataItem) => ReactNode | undefined {
        const MenuGroup = type === 'context' ? ContextMenuGroup : DropdownMenuGroup
        const MenuItem = type === 'context' ? ContextMenuItem : DropdownMenuItem

        return function DisplayMenu(item: TreeDataItem) {
            if (item.id.startsWith('project-folder-empty/')) {
                return undefined
            }
            if (item.record?.type === 'folder') {
                return (
                    <MenuGroup>
                        <MenuItem
                            asChild
                            onClick={(e) => {
                                e.stopPropagation()
                                createFolder(item.record?.path || '', true, (folder) => {
                                    onChange?.(folder)
                                })
                            }}
                        >
                            <ButtonPrimitive menuItem>New folder</ButtonPrimitive>
                        </MenuItem>
                        {item.record?.path && item.record?.type === 'folder' ? (
                            <MenuItem
                                asChild
                                onClick={(e) => {
                                    e.stopPropagation()
                                    setEditingItemId(item.id)
                                }}
                            >
                                <ButtonPrimitive menuItem>Rename</ButtonPrimitive>
                            </MenuItem>
                        ) : null}
                    </MenuGroup>
                )
            }
            return undefined
        }
    }

    return (
        <div className="flex flex-col gap-2">
            <LemonInput
                type="search"
                placeholder="Search"
                fullWidth
                onChange={(search) => setSearchTerm(search)}
                value={searchTerm}
            />
            <div className={clsx('bg-surface-primary p-2 border rounded-[var(--radius)] overflow-y-scroll', className)}>
                <LemonTree
                    ref={treeRef}
                    selectMode="folder-only"
                    className="px-0 py-1"
                    data={fullFileSystemFiltered}
                    mode="tree"
                    tableViewKeys={treeTableKeys}
                    defaultSelectedFolderOrNodeId={
                        value?.includes('://') ? value : value ? 'project://' + value : undefined
                    }
                    isItemActive={(item) => item.record?.path === value}
                    isItemEditing={(item) => {
                        return editingItemId === item.id
                    }}
                    onItemNameChange={(item, name) => {
                        if (item.name !== name) {
                            rename(name, item.record as unknown as FileSystemEntry)
                        }
                        // Clear the editing item id when the name changes
                        setEditingItemId('')
                    }}
                    showFolderActiveState={true}
                    checkedItemCount={0}
                    onFolderClick={(folder, isExpanded) => {
                        if (folder) {
                            if (includeProtocol) {
                                setSelectedFolder(folder.id)
                                toggleFolderOpen(folder.id, isExpanded)
                                onChange?.(folder.id)
                            } else {
                                setSelectedFolder(folder.record?.path)
                                toggleFolderOpen(folder.id || '', isExpanded)
                                onChange?.(folder.record?.path ?? '')
                            }
                        }
                    }}
                    renderItem={(item) => {
                        return (
                            <span>
                                {item.record?.path === selectedFolder ? (
                                    <span className="flex items-center gap-1">
                                        {item.displayName}
                                        <IconCheckCircle className="size-4 text-success" />
                                    </span>
                                ) : (
                                    item.displayName
                                )}
                            </span>
                        )
                    }}
                    expandedItemIds={searchTerm ? expandedSearchFolders : expandedFolders}
                    onSetExpandedItemIds={searchTerm ? setExpandedSearchFolders : setExpandedFolders}
                    enableDragAndDrop={false}
                    itemContextMenu={getItemContextMenu('context')}
                    itemSideAction={getItemContextMenu('dropdown')}
                    emptySpaceContextMenu={() => {
                        return (
                            <ContextMenuGroup>
                                <ContextMenuItem
                                    asChild
                                    onClick={(e) => {
                                        e.stopPropagation()
                                        createFolder('', true)
                                    }}
                                >
                                    <ButtonPrimitive menuItem>New folder</ButtonPrimitive>
                                </ContextMenuItem>
                            </ContextMenuGroup>
                        )
                    }}
                />
            </div>
        </div>
    )
}

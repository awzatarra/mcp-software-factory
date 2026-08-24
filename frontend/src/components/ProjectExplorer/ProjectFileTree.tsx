import {
  ChevronDown,
  ChevronRight,
  File,
  FileCode2,
  Folder,
  FolderOpen,
} from "lucide-react";
import { useState } from "react";

import type { ProjectFileNode } from "../../api/types";

function TreeNode({
  node,
  selectedPath,
  onSelect,
  level,
}: {
  node: ProjectFileNode;
  selectedPath: string | null;
  onSelect: (node: ProjectFileNode) => void;
  level: number;
}) {
  const [expanded, setExpanded] = useState(level < 2);
  const directory = node.type === "directory";
  const children = node.children ?? [];
  return (
    <li role="treeitem" aria-expanded={directory ? expanded : undefined}>
      <button
        type="button"
        className={`tree-node ${selectedPath === node.path ? "selected" : ""}`}
        style={{ paddingLeft: `${8 + level * 16}px` }}
        onClick={() => directory ? setExpanded((value) => !value) : onSelect(node)}
      >
        {directory ? (
          expanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />
        ) : <span className="tree-spacer" />}
        {directory ? (
          expanded ? <FolderOpen aria-hidden="true" /> : <Folder aria-hidden="true" />
        ) : node.content_type === "text" ? (
          <FileCode2 aria-hidden="true" />
        ) : (
          <File aria-hidden="true" />
        )}
        <span>{node.name}</span>
        {node.is_generated ? <small>Nuevo</small> : null}
        {node.is_updated ? <small>Modificado</small> : null}
      </button>
      {directory && expanded && children.length ? (
        <ul role="group">
          {children.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              selectedPath={selectedPath}
              onSelect={onSelect}
              level={level + 1}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function ProjectFileTree({
  root,
  selectedPath,
  onSelect,
}: {
  root: ProjectFileNode;
  selectedPath: string | null;
  onSelect: (node: ProjectFileNode) => void;
}) {
  return (
    <ul className="project-file-tree" role="tree" aria-label="Archivos del proyecto">
      <TreeNode
        node={root}
        selectedPath={selectedPath}
        onSelect={onSelect}
        level={0}
      />
    </ul>
  );
}

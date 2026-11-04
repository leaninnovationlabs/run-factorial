/**
 * An editor card (`factorial_editor_open`): a process draft the assistant prepared for a person to
 * review and commit in the process editor, because only a person may commit it. The press opens
 * the project's process config page at `?draft=`, which loads it into the editor
 * (`useEditorDraft`).
 */

import { useNavigate } from "react-router-dom";
import type { ChatItem } from "../../../api/types";
import { configurationHref } from "../../project/place";
import { Button } from "../../ui";
import { useAssistant } from "../AssistantProvider";

export function EditorDraftChip({ item }: { item: ChatItem }) {
  const { close } = useAssistant();
  const navigate = useNavigate();
  const { project_id: projectId, process, would_write: files } = item.payload;
  if (typeof projectId !== "number") return null;
  const count = Array.isArray(files) ? files.length : 0;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-default bg-surface p-3">
      <Button
        iconBefore="edit"
        onClick={() => {
          close();
          navigate(`${configurationHref(projectId, "process")}?draft=${item.id}`);
        }}
      >
        Open in the process editor
      </Button>
      <span className="min-w-0 text-body text-subtle">
        {typeof process === "string" ? process : "A process"} · {count}{" "}
        {count === 1 ? "file" : "files"}
      </span>
    </div>
  );
}

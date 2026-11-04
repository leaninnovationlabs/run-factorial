/**
 * The runtime's metadata, as a table of key/value rows with one Save for the whole map.
 *
 * One Save rather than one per row because the API replaces the map whole (`RuntimePatch`):
 * a per-row write would race the next row's, and a row removed would have to be a write of
 * everything else anyway. The rows are the operator's draft until Save; a refetch that brings
 * a different map from the server resets the draft, one that brings the same map leaves it.
 *
 * Nothing here reaches a run — `metadata` is notes on the row, not configuration of it.
 */

import { useEffect, useState } from "react";
import { useUpdateRuntime } from "../../api/queries";
import type { Runtime } from "../../api/types";
import {
  Button,
  ErrorMessage,
  Form,
  FormFooter,
  IconButton,
  SectionMessage,
  showFlag,
  TextField,
} from "../ui";
import {
  METADATA_MAX_KEYS,
  type MetadataRow,
  metadataProblem,
  metadataToRows,
  rowsToMetadata,
} from "./runtimes";

export function RuntimeMetadata({ runtime }: { runtime: Runtime }) {
  const update = useUpdateRuntime(runtime.id);
  const [rows, setRows] = useState<MetadataRow[]>(() => metadataToRows(runtime.metadata));
  // Ids from the server's map are 0..n-1; an added row takes the next one so it never collides.
  const [nextId, setNextId] = useState(rows.length);

  const stored = JSON.stringify(runtime.metadata);
  useEffect(() => {
    const fresh = metadataToRows(JSON.parse(stored));
    setRows(fresh);
    setNextId(fresh.length);
  }, [stored]);

  const problem = metadataProblem(rows);
  const dirty = JSON.stringify(rowsToMetadata(rows)) !== stored;

  function edit(id: number, field: "key" | "value", text: string) {
    setRows((current) => current.map((row) => (row.id === id ? { ...row, [field]: text } : row)));
  }

  function add() {
    setRows((current) => [...current, { id: nextId, key: "", value: "" }]);
    setNextId(nextId + 1);
  }

  function remove(id: number) {
    setRows((current) => current.filter((row) => row.id !== id));
  }

  return (
    <Form
      validate={() => problem.kind === "ok"}
      onSubmit={() => {
        if (!dirty) {
          showFlag({ appearance: "info", title: "No changes to save" });
          return;
        }
        update.mutate(
          { metadata: rowsToMetadata(rows) },
          { onSuccess: () => showFlag({ appearance: "success", title: "Saved the metadata" }) },
        );
      }}
      className="space-y-3 p-4"
    >
      <p className="text-body text-subtle">
        Notes on this runtime for the people who run it. Nothing here reaches an agent run.
      </p>

      {rows.length > 0 && (
        <div className="space-y-2">
          <div
            aria-hidden="true"
            className="grid grid-cols-[1fr_2fr_auto] gap-2 text-body-sm font-semibold text-subtle"
          >
            <span>Key</span>
            <span>Value</span>
            <span className="w-8" />
          </div>
          {rows.map((row, index) => {
            const fault = problem.kind === "row" && problem.id === row.id ? problem : null;
            return (
              <div key={row.id} className="grid grid-cols-[1fr_2fr_auto] items-start gap-2">
                <TextField
                  label={`Key, row ${index + 1}`}
                  labelHidden
                  name="metadata-key"
                  value={row.key}
                  onChange={(event) => edit(row.id, "key", event.target.value)}
                  spellCheck={false}
                  autoComplete="off"
                  isMonospaced
                  errorMessage={fault?.field === "key" ? fault.message : undefined}
                />
                <TextField
                  label={`Value, row ${index + 1}`}
                  labelHidden
                  name="metadata-value"
                  value={row.value}
                  onChange={(event) => edit(row.id, "value", event.target.value)}
                  spellCheck={false}
                  autoComplete="off"
                  errorMessage={fault?.field === "value" ? fault.message : undefined}
                />
                <IconButton
                  icon="trash"
                  label={`Remove row ${index + 1}`}
                  onClick={() => remove(row.id)}
                />
              </div>
            );
          })}
        </div>
      )}

      {problem.kind === "map" && (
        <SectionMessage appearance="error">{problem.message}</SectionMessage>
      )}
      <ErrorMessage error={update.error} />

      <FormFooter>
        <Button
          iconBefore="plus"
          onClick={add}
          disabledReason={
            rows.length >= METADATA_MAX_KEYS
              ? `A runtime holds at most ${METADATA_MAX_KEYS} keys.`
              : undefined
          }
        >
          Add row
        </Button>
        <Button type="submit" appearance="primary" isLoading={update.isPending}>
          Save metadata
        </Button>
      </FormFooter>
    </Form>
  );
}

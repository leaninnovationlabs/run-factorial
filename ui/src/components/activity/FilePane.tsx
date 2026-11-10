/**
 * A file a step declares (decision 71): a file its agent committed on its branch, as the API serves
 * it by its extension (decision 58), framed beside the run's transcript, or why there is none.
 *
 * The frame gets no scripts, no same-origin and no forms; the API's CSP says the same, and either
 * alone keeps an agent-written file inert. Popups are allowed only so its links open in a new
 * tab, where the reader has an address bar (design.md §7.15). The `src` is a signed link that
 * changes on the hour, so the pane keeps the one it mounted with — a poll must not reload a file
 * someone is reading — while Reload and New tab take the newest. The API also wants the session
 * cookie, which every reader of the dashboard holds.
 *
 * Two waits are told apart, because only one of them is loading anything: a file whose attempt is
 * still going is not recorded yet, while one whose attempt has ended is being read from the forge
 * by the worker, and a framed file is read from the forge by the API on every load.
 *
 * A missing file lists the paths the step did leave on its branch, so whoever wrote the config can
 * correct it. They are paths and forge links only: nothing in the list is served.
 */

import { useState } from "react";
import { apiUrl } from "../../api/client";
import type { ChangedFile, OutputEntry, StepRun } from "../../api/types";
import { type ForgeHost, forgeName } from "../../forge";
import { shortSha } from "../../format";
import { Button, EmptyState, LinkButton, Spinner, Tooltip } from "../ui";
import { hasEnded } from "./runs";

const FILE_SANDBOX = "allow-popups allow-popups-to-escape-sandbox";

/** A sentence from a reason the API wrote in lower case. */
function sentence(text: string): string {
  const capital = `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
  return capital.endsWith(".") ? capital : `${capital}.`;
}

export function FilePane({
  file,
  run,
  step,
  changed,
  forge,
}: {
  file: OutputEntry;
  run: StepRun;
  step: string;
  /** The paths the step left on its branch (`RunOutputs.changed_files`). */
  changed: ChangedFile[];
  forge?: ForgeHost;
}) {
  const host = forgeName(forge);
  const [framed, setFramed] = useState(() => (file.url ? apiUrl(file.url) : null));
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const src = framed;
  const link = file.url;
  const named =
    file.status === "published" && file.path && file.commit
      ? `${file.path} at ${shortSha(file.commit)}${file.changed === false ? " (unchanged in this attempt)" : ""}`
      : file.status === "missing"
        ? "File not found"
        : null;
  const name = (
    <span className="min-w-0 flex-1 truncate font-mono text-body-sm text-subtle">{named}</span>
  );

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 border-b border-default px-4 py-2">
        {named ? (
          <Tooltip content={named} position="bottom-start" onlyIfTruncated>
            {name}
          </Tooltip>
        ) : (
          name
        )}
        {src && link && (
          <>
            <Button
              spacing="small"
              iconBefore="refresh"
              onClick={() => {
                setFramed(apiUrl(link));
                setReloads((count) => count + 1);
                setLoaded(false);
              }}
            >
              Reload
            </Button>
            <LinkButton href={apiUrl(link)} spacing="small">
              New tab
            </LinkButton>
          </>
        )}
        {file.github_url && (
          <LinkButton href={file.github_url} spacing="small">
            {host}
          </LinkButton>
        )}
      </div>
      {src ? (
        <div className="relative flex min-h-0 flex-1 flex-col">
          <iframe
            key={reloads}
            src={src}
            title={`${step} ${file.path}, attempt ${run.attempt}`}
            sandbox={FILE_SANDBOX}
            referrerPolicy="no-referrer"
            onLoad={() => setLoaded(true)}
            className="min-h-0 w-full flex-1 border-0"
          />
          {!loaded && (
            <div className="absolute inset-0 flex items-center justify-center bg-surface">
              <Spinner inline label={`Loading the file from ${host}…`} />
            </div>
          )}
        </div>
      ) : file.status === "pending" && hasEnded(run) ? (
        <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
          <Spinner inline label="Loading the file…" />
          <p className="text-body-sm text-subtle">
            Attempt {run.attempt} has finished. Its file appears here in a moment.
          </p>
        </div>
      ) : (
        <>
          <EmptyState headingLevel={3} {...whyNoFrame(file, run, host)} />
          {file.status === "missing" && changed.length > 0 && <ChangedFiles files={changed} />}
        </>
      )}
    </>
  );
}

function ChangedFiles({ files }: { files: ChangedFile[] }) {
  return (
    <section className="mx-auto w-full max-w-xl px-4 pb-6">
      <h4 className="text-body font-medium text-default">
        Files this step changed ({files.length})
      </h4>
      <p className="mt-1 text-body-sm text-subtle">
        If one of these is the file you expected, fix its path in the step's preview settings.
      </p>
      <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto">
        {files.map((changedFile) => (
          <li key={changedFile.path} className="break-all">
            <a
              href={changedFile.github_url}
              target="_blank"
              rel="noreferrer"
              className="focus-inset font-mono text-body-sm font-medium hover:underline"
            >
              {changedFile.path}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

function whyNoFrame(
  file: OutputEntry,
  run: StepRun,
  host: string,
): { header: string; description: string } {
  switch (file.status) {
    case "pending":
      return {
        header: "Not ready yet",
        description: `This file appears here when attempt ${run.attempt} finishes.`,
      };
    case "missing":
      return {
        header: "File not found",
        description: file.reason
          ? sentence(file.reason)
          : `Attempt ${run.attempt} didn't create or change this file.`,
      };
    case "ambiguous":
      return {
        header: "More than one file matches",
        description: `The path matches ${file.matches.length} files: ${file.matches.join(", ")}. Make the path in the process config more specific.`,
      };
    case "lookup_failed":
      return {
        header: `Couldn't load this file from ${host}`,
        description: file.reason ? sentence(file.reason) : "Try again in a minute.",
      };
    case "published":
      return file.too_large
        ? {
            header: "The file is over 1 MB",
            description: `Files over 1 MB can't be shown here. Open it on ${host}.`,
          }
        : {
            header: "The file has no link",
            description: `This file can't be shown here.${file.github_url ? ` Open it on ${host}.` : ""}`,
          };
  }
}

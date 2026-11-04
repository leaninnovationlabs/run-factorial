/**
 * What the operator permits this project (tech-stack decision 25, design.md §7.5): the E2B
 * runtimes it may name, the extra hosts its sandboxes may reach, and the MCP servers it may run.
 * The repo's `.factorial/` selects; these permit. They are operator state in `project.settings`,
 * and the repo can never write them — which is why they are set here and not read from the repo.
 *
 * Every change saves on its own, with an Undo: a list with a Save button left a ticked box that
 * might or might not be permitted yet. A change is sent as what changed and applied on the server
 * (`PATCH /projects/{id}/permits`), so a save built from a stale page cannot drop another, and an
 * Undo is the reverse change rather than an old copy of the settings.
 *
 * A reader without `projects:manage` sees every permit and changes none. The runtimes themselves
 * are listed only for one who holds `runtimes:manage`; anyone else sees the permitted slugs alone.
 */

import { useState } from "react";
import { useCan } from "../../api/organizationQueries";
import { useChangePermits, useRuntimes } from "../../api/queries";
import type {
  ListChange,
  PermitsChange,
  ProjectDetail,
  ReadinessItem,
  Runtime,
} from "../../api/types";
import { DOCS } from "../../docs";
import { plural } from "../../format";
import { unpermitted } from "../process/commitGate";
import { isolationOf, RUNTIME_TYPE_LABELS } from "../runtimes/runtimes";
import {
  Button,
  Checkbox,
  Code,
  CodeBlock,
  ErrorMessage,
  Icon,
  showFlag,
  Tag,
  TagGroup,
  TextField,
} from "../ui";
import { isWorse, READINESS, readinessOf } from "./readiness";

export const RUNTIME_ALLOWLIST = "runtime_allowlist";
export const EGRESS_ALLOWLIST = "egress_allowlist";
const MCP_ALLOWLIST = "mcp_allowlist";

//: The server's rule (`runtime_policy._HOST`), so a bad host is flagged as it is typed rather
//: than by a 422 after it is sent. The server still decides.
const HOST = /^(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]([a-z0-9-]*[a-z0-9])?$/;

export function listSetting(settings: Record<string, unknown>, key: string): string[] {
  const value = settings[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function reversed(change: PermitsChange): PermitsChange {
  const flip = (list?: ListChange) => list && { add: list.remove, remove: list.add };
  return {
    runtime_allowlist: flip(change.runtime_allowlist),
    egress_allowlist: flip(change.egress_allowlist),
  };
}

/** Apply one permit change; its confirmation's Undo applies the reverse. Shared with the
 *  runtime row's one-click permit, so both save the same way. */
export function usePermitChange(projectId: number) {
  const mutation = useChangePermits(projectId);
  const apply = (change: PermitsChange, title: string, description?: string) =>
    mutation.mutate(change, {
      onSuccess: () =>
        showFlag({
          appearance: "success",
          title,
          description,
          actions: [{ text: "Undo", onClick: () => mutation.mutate(reversed(change)) }],
        }),
    });
  return { apply, isPending: mutation.isPending, error: mutation.error };
}

/** Lanes by the runtime slug they name, off the project's own runtime readiness rows. */
function lanesBySlug(project: ProjectDetail): Map<string, string[]> {
  const lanes = new Map<string, string[]>();
  for (const row of project.readiness) {
    if (row.key !== "runtime" || !row.runtime_slug || !row.process) continue;
    lanes.set(row.runtime_slug, [...(lanes.get(row.runtime_slug) ?? []), row.process]);
  }
  return lanes;
}

/** The section's header line: what is permitted, and a warning when a lane names an E2B runtime
 *  this project does not permit — the runtime row's refusal, seen from this side. `runtimes` is
 *  null for a reader who may not list them. */
export function permitsStatus(
  project: ProjectDetail,
  runtimes: Runtime[] | null | undefined,
): { state: ReadinessItem["state"]; summary: string } {
  // Which permitted slugs are E2B, and which lanes need one, is unknowable without the runtimes.
  if (runtimes === undefined) return { state: "unknown", summary: "Loading the runtimes…" };
  const allowed = listSetting(project.settings, RUNTIME_ALLOWLIST);
  const hosts = listSetting(project.settings, EGRESS_ALLOWLIST);
  const unpermittedSlugs =
    runtimes === null ? [] : unpermitted([...lanesBySlug(project).keys()], runtimes, allowed);
  if (unpermittedSlugs.length > 0) {
    return {
      state: "warn",
      summary: `${unpermittedSlugs.join(", ")} ${
        unpermittedSlugs.length === 1 ? "is used by a process" : "are used by processes"
      } but not permitted`,
    };
  }
  const parts = [
    allowed.length === 0
      ? "No cloud sandbox runtime"
      : plural(allowed.length, "cloud sandbox runtime"),
    hosts.length === 0 ? "no extra hosts" : plural(hosts.length, "extra host"),
  ];
  return { state: runtimes === null ? "unknown" : "ok", summary: parts.join(" · ") };
}

/** The Runtime page's state: the worse of its runtime row and the permits, so a runtime that
 *  cannot run and a lane naming one the project does not permit both reach the page's header and
 *  the overview. */
export function runTimeStatus(
  project: ProjectDetail,
  row: ReadinessItem | null,
  runtimes: Runtime[] | null | undefined,
) {
  const permits = permitsStatus(project, runtimes);
  if (row && !isWorse(permits.state, row.state)) {
    return { ...readinessOf(row.key, row.state), summary: row.summary };
  }
  return { ...READINESS[permits.state], summary: permits.summary };
}

function Subsection({
  title,
  note,
  children,
}: {
  title: string;
  note: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2 px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <h3 className="text-body-sm font-semibold text-subtle">{title}</h3>
        <p className="text-body-sm text-subtlest">{note}</p>
      </div>
      {children}
    </section>
  );
}

export function PermitsBody({ project }: { project: ProjectDetail }) {
  const canManage = useCan("projects:manage");
  const canRuntimes = useCan("runtimes:manage");
  const runtimes = useRuntimes({ enabled: canRuntimes });
  const permits = usePermitChange(project.id);
  const [host, setHost] = useState("");

  const allowed = listSetting(project.settings, RUNTIME_ALLOWLIST);
  const hosts = listSetting(project.settings, EGRESS_ALLOWLIST);
  const lanes = lanesBySlug(project);
  const e2b = (runtimes.data ?? []).filter((rt) => isolationOf(rt.details) === "e2b");
  // A slug permitted earlier whose runtime was since deleted stays listed, so it can be removed
  // on purpose rather than lingering unseen. Only once the runtimes have loaded: before that,
  // every permitted slug would look deleted.
  const orphaned = runtimes.data
    ? allowed.filter((slug) => !e2b.some((rt) => rt.slug === slug))
    : [];
  const mcp = project.settings[MCP_ALLOWLIST];

  const typed = host.trim().toLowerCase();
  const hostError =
    typed === ""
      ? undefined
      : !HOST.test(typed)
        ? "Enter a host such as registry.npmjs.org or *.example.com. Addresses and ranges are refused."
        : hosts.includes(typed)
          ? "Already permitted."
          : undefined;

  const setRuntime = (slug: string, on: boolean) =>
    permits.apply(
      { runtime_allowlist: on ? { add: [slug] } : { remove: [slug] } },
      on ? `Permitted ${slug}` : `No longer permitting ${slug}`,
    );

  const addHost = () => {
    if (typed === "" || hostError) return;
    permits.apply({ egress_allowlist: { add: [typed] } }, `Added ${typed}`);
    setHost("");
  };

  const removeHost = (name: string) =>
    permits.apply({ egress_allowlist: { remove: [name] } }, `Removed ${name}`);

  return (
    <div className="divide-y divide-default">
      <p className="px-4 py-3 text-body text-subtle">
        The repository names a runtime in <Code>.factorial/</Code>. A cloud sandbox runs only once
        it is permitted here.
      </p>

      <Subsection title="Cloud sandbox runtimes" note="Host runtimes need no permit">
        {!canRuntimes ? (
          allowed.length === 0 ? (
            <p className="text-body-sm text-subtle">None permitted.</p>
          ) : (
            <TagGroup label="Permitted cloud sandbox runtimes">
              {allowed.map((slug) => (
                <Tag key={slug}>{slug}</Tag>
              ))}
            </TagGroup>
          )
        ) : runtimes.isPending ? (
          <p className="text-body-sm text-subtle">Loading the runtimes…</p>
        ) : runtimes.error ? (
          <ErrorMessage error={runtimes.error} />
        ) : e2b.length === 0 && orphaned.length === 0 ? (
          <p className="text-body-sm text-subtle">
            No runtime runs in a cloud sandbox yet. Create one on the Runtimes page to permit it
            here.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {e2b.map((rt) => {
              const usedBy = lanes.get(rt.slug) ?? [];
              const needed = usedBy.length > 0 && !allowed.includes(rt.slug);
              return (
                <li key={rt.slug} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <Checkbox
                    label={rt.slug}
                    isChecked={allowed.includes(rt.slug)}
                    isDisabled={!canManage || permits.isPending}
                    onChange={(on) => setRuntime(rt.slug, on)}
                  />
                  <span className="text-body-sm text-subtle">{RUNTIME_TYPE_LABELS[rt.type]}</span>
                  <span className="inline-flex items-center gap-1 text-body-sm text-subtlest">
                    {needed && (
                      <Icon
                        glyph="status-warning"
                        label="Used by a process but not permitted"
                        className="text-icon-warning"
                      />
                    )}
                    {usedBy.length > 0 ? (
                      <>
                        used by{" "}
                        {usedBy.map((lane) => (
                          <Code key={lane}>{lane}</Code>
                        ))}
                      </>
                    ) : (
                      "not used"
                    )}
                  </span>
                </li>
              );
            })}
            {orphaned.map((slug) => (
              <li key={slug} className="flex flex-wrap items-center gap-x-3">
                <Checkbox
                  label={slug}
                  isChecked
                  isDisabled={!canManage || permits.isPending}
                  onChange={(on) => setRuntime(slug, on)}
                />
                <span className="text-body-sm text-subtlest">no longer exists</span>
              </li>
            ))}
          </ul>
        )}
      </Subsection>

      <Subsection title="Egress hosts" note="Always reachable: GitHub, the model API, MCP servers">
        {hosts.length === 0 ? (
          <p className="text-body-sm text-subtle">None added.</p>
        ) : (
          <TagGroup label="Permitted egress hosts">
            {hosts.map((name) => (
              <Tag
                key={name}
                onRemove={canManage ? () => removeHost(name) : undefined}
                removeLabel={`Remove ${name}`}
              >
                {name}
              </Tag>
            ))}
          </TagGroup>
        )}
        {canManage && (
          <form
            className="flex items-start gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              addHost();
            }}
          >
            <div className="min-w-0 max-w-sm flex-1">
              <TextField
                label="Add a host"
                labelHidden
                value={host}
                onChange={(event) => setHost(event.target.value)}
                placeholder="Add a host, such as registry.npmjs.org"
                errorMessage={hostError}
              />
            </div>
            <Button type="submit" isDisabled={typed === "" || !!hostError || permits.isPending}>
              Add
            </Button>
          </form>
        )}
      </Subsection>

      <Subsection title="MCP servers" note="Read-only here">
        {Array.isArray(mcp) && mcp.length > 0 ? (
          <CodeBlock text={JSON.stringify(mcp, null, 2)} label="The project's MCP allowlist" />
        ) : (
          <p className="text-body-sm text-subtle">
            No MCP servers are permitted. Add them through the API. See{" "}
            <a
              href={`${DOCS}/mcp-servers/#permit`}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2"
            >
              MCP servers
            </a>{" "}
            in the docs.
          </p>
        )}
      </Subsection>

      {permits.error && (
        <div className="px-4 py-3">
          <ErrorMessage error={permits.error} />
        </div>
      )}
    </div>
  );
}

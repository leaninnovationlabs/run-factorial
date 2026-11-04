/**
 * The factory board's domain model: what a node is, which lane it falls in, and what the shade
 * modes shade.
 *
 * The lanes are deliberately NOT the ADLC steps. Every project supplies its own
 * `.factorial/process.yaml`, two projects need not share step names, and editing a config creates a
 * new `process_version` that in-flight tickets do not adopt — so a step-keyed column cannot mean
 * the same thing for every node on a board that spans projects. `TicketStatus` plus the run
 * counts is defined by the orchestrator rather than by any project's config, and partitions
 * every ticket into exactly one lane wherever it came from.
 *
 * `IssueNode` is a projection of the overview read model: the lane inputs plus the in-flight
 * run's attempt number and the three spend totals the shade modes rank and colour by. See
 * `source.ts` for the mapping.
 */

export * from "./lanes";
export * from "./metrics";
export * from "./modes";

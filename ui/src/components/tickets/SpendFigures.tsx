import { InlineMessage, Metric } from "../ui";

/**
 * What a ticket or a project has spent, stated the same way wherever it is stated: each figure
 * boxed with its unit and captioned in words. The captions were once only in native tooltips,
 * which a keyboard or a touch screen never shows.
 *
 * An estimate keeps its markers (`~`, "est.") and says why in an explanation behind an icon: a
 * codex attempt is priced at list, which is not what the bill will say (decision 34).
 */
export function SpendFigures({
  tokens,
  cost,
  estimated,
  time,
}: {
  tokens: string;
  cost: string;
  estimated: boolean;
  /** Time spent in runs, for a ticket; a project's runs overlap, so it has none. */
  time?: string;
}) {
  return (
    <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
      <Metric label="Tokens">{tokens}</Metric>
      <span className="inline-flex items-center gap-1">
        <Metric label="Cost">{cost}</Metric>
        {estimated && <EstimateNote />}
      </span>
      {time && <Metric label="Time">{time}</Metric>}
    </span>
  );
}

/** Why a figure marked as an estimate is one, behind an icon. */
export function EstimateNote() {
  return (
    <InlineMessage iconLabel="Why the cost is an estimate" title="Estimated cost">
      At least one attempt ran on a model that reports no price, so it is priced at the model's list
      rates. The total can differ from what the provider bills.
    </InlineMessage>
  );
}

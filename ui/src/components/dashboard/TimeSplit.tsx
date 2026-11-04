import type { TimeSplitFigures } from "../../api/types";
import { formatDuration, plural } from "../../format";
import { EmptyState, Panel, StackedBar } from "../ui";

/**
 * One bar, part to whole, of the lead time of the tickets done in the window. Gate waits and
 * runs paused mid-step are one segment because both wait on a person; its legend entry splits
 * them. Shares are of the three parts' sum, not of `lead_seconds`: idle is floored at zero per
 * ticket, so the parts can outrun the lead time.
 */
export function TimeSplit({ split }: { split: TimeSplitFigures }) {
  const waiting = split.gate_seconds + split.paused_seconds;
  const whole = split.agent_seconds + waiting + split.idle_seconds;
  const share = (seconds: number) => `${whole > 0 ? Math.round((seconds / whole) * 100) : 0}%`;
  const figure = (seconds: number) => `${share(seconds)} · ${formatDuration(seconds)}`;
  return (
    <Panel
      title="Where the time goes"
      subtitle="Lead time of the done tickets, split three ways."
      note="Agents working covers agent, check and action runs, without the time they sat paused. Waiting for people is gate waits plus runs paused for a person to answer or resume them. Idle is the rest: queued, blocked, or between runs."
    >
      {split.tickets === 0 ? (
        <EmptyState header="No ticket is done yet" headingLevel={3} />
      ) : (
        <div className="px-4 py-4">
          <StackedBar
            label={`Lead time of ${plural(split.tickets, "done ticket")}`}
            segments={[
              {
                key: "agent",
                look: "series-1",
                label: "Agents working",
                value: split.agent_seconds,
                text: figure(split.agent_seconds),
              },
              {
                key: "waiting",
                look: "series-2",
                label: "Waiting for people",
                value: waiting,
                text: `${figure(waiting)} (at gates ${share(split.gate_seconds)}, paused mid-run ${share(split.paused_seconds)})`,
              },
              {
                key: "idle",
                look: "idle",
                label: "Idle",
                value: split.idle_seconds,
                text: figure(split.idle_seconds),
              },
            ]}
          />
        </div>
      )}
    </Panel>
  );
}

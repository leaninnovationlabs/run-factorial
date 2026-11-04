import { Link } from "react-router-dom";
import { Tooltip } from "../ui";

/** `#67`, to the ticket page, the way `TicketTable` writes a key. */
export function TicketLink({ id, external_key }: { id: number; external_key: string }) {
  return (
    <Link to={`/tickets/${id}`} className="font-medium hover:underline">
      #{external_key}
    </Link>
  );
}

/**
 * A bar's label: the key as a link, then `tip` (the title unless the caller adds to it) as text.
 * The text shows below `sm`, where there is no hover; from `sm` up it is in the key's tooltip,
 * and stays in the markup for a screen reader, which a tooltip does not reach.
 */
export function TicketLabel({
  id,
  external_key,
  title,
  tip = title ?? undefined,
}: {
  id: number;
  external_key: string;
  title: string | null;
  tip?: string;
}) {
  const link = (
    <Link to={`/tickets/${id}`} className="font-medium hover:underline">
      #{external_key}
    </Link>
  );
  return (
    <span className="flex min-w-0 items-center gap-2">
      {tip ? (
        <Tooltip content={tip} position="top-start">
          {link}
        </Tooltip>
      ) : (
        link
      )}
      {tip && <span className="truncate text-subtle sm:sr-only">{tip}</span>}
    </span>
  );
}

import { type ForgeHost, forgeName, repoHref } from "../../forge";
import { Code, Icon } from "../ui";

export function RepoLink({ repo, forge }: { repo: string; forge?: ForgeHost }) {
  return (
    <a
      href={repoHref(repo, forge)}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 hover:underline"
    >
      <Code>{repo}</Code>
      <Icon
        glyph="link-external"
        size="small"
        label={`Opens ${forgeName(forge)}`}
        className="text-icon-subtle"
      />
    </a>
  );
}

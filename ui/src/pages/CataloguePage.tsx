/** The catalogue reference page (docs/design-system.md, Reference pages). */

import { Button, PageHeader, StatusLozenge, usePageTitle } from "../components/ui";
import { Buttons, Fields, Icons, Lozenges, Messages } from "../components/ui/catalogue";
import {
  CodeAndFigures,
  Figures,
  Navigation,
  Overlays,
  States,
  Table,
} from "../components/ui/catalogueComposites";
import { ReferenceNav } from "./ReferenceNav";

export default function CataloguePage() {
  usePageTitle(["Catalogue"]);
  return (
    <div className="min-h-dvh space-y-6 bg-surface-sunken px-4 py-6 lg:px-8">
      <PageHeader
        title="Catalogue"
        actions={
          <>
            <ReferenceNav current="catalogue" />
            <Button appearance="primary">The one primary action</Button>
          </>
        }
        bottomBar={
          <>
            <StatusLozenge status="running" />
            <span>Every design primitive, in every state</span>
          </>
        }
      />
      <Icons />
      <Buttons />
      <Lozenges />
      <Messages />
      <Fields />
      <Navigation />
      <States />
      <CodeAndFigures />
      <Figures />
      <Overlays />
      <Table />
    </div>
  );
}

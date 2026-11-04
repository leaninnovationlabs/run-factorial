// @vitest-environment happy-dom
/**
 * A form whose submit button stays pressable: it checks on submit, sends nothing that is wrong,
 * and puts the focus on the first field that says so.
 */

import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, settle, type } from "../../testDom";
import { Button } from "./Button";
import { Form } from "./form";
import { TextField } from "./TextField";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

function CreateRuntime({ onSubmit }: { onSubmit: (form: HTMLFormElement) => void }) {
  const [name, setName] = useState("codex team");
  const [slug, setSlug] = useState("");
  const [token, setToken] = useState("");
  return (
    <Form validate={() => name.trim() !== "" && slug.trim() !== ""} onSubmit={onSubmit}>
      {({ attempted }) => (
        <>
          <TextField
            label="Name"
            name="name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            errorMessage={attempted && name.trim() === "" ? "A runtime needs a name." : undefined}
          />
          <TextField
            label="Slug"
            name="slug"
            value={slug}
            onChange={(event) => setSlug(event.target.value)}
            errorMessage={attempted && slug.trim() === "" ? "A runtime needs a slug." : undefined}
          />
          <TextField
            label="Token"
            name="token"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
          <Button type="submit" appearance="primary">
            Create runtime
          </Button>
        </>
      )}
    </Form>
  );
}

/** A form that empties itself once it has sent, as storing a secret or a credential does. */
function StoreValue() {
  const [value, setValue] = useState("s3cret");
  return (
    <Form validate={() => value.trim() !== ""} onSubmit={() => setValue("")}>
      {({ attempted }) => (
        <>
          <TextField
            label="Value"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            errorMessage={attempted && value.trim() === "" ? "Enter a value to store." : undefined}
          />
          <Button type="submit" appearance="primary">
            Store
          </Button>
        </>
      )}
    </Form>
  );
}

const field = (label: string) => {
  const id = byText(document, "label", label)?.getAttribute("for") ?? "";
  return document.getElementById(id) as HTMLInputElement;
};
const submit = () => byText(document, "button", "Create runtime") as HTMLButtonElement;

describe("Form", () => {
  it("does not mark the fields it just emptied after a submit that went through", async () => {
    view = await mount(<StoreValue />);
    await click(byText(document, "button", "Store"));
    await settle(40);
    expect(field("Value").value).toBe("");
    expect(field("Value").hasAttribute("aria-invalid")).toBe(false);
    expect(document.body.textContent).not.toContain("Enter a value to store.");
  });

  it("keeps the submit button pressable while fields are empty", async () => {
    view = await mount(<CreateRuntime onSubmit={vi.fn()} />);
    expect(submit().disabled).toBe(false);
    expect(submit().hasAttribute("aria-disabled")).toBe(false);
  });

  it("sends nothing that is wrong, marks what is, and focuses the first wrong field", async () => {
    const onSubmit = vi.fn();
    view = await mount(<CreateRuntime onSubmit={onSubmit} />);
    await type(field("Name"), "");
    await click(submit());
    await settle(40);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(field("Name").getAttribute("aria-invalid")).toBe("true");
    expect(field("Slug").getAttribute("aria-invalid")).toBe("true");
    expect(field("Token").hasAttribute("aria-invalid")).toBe(false);
    // The first one that is wrong, in the order the form reads.
    expect(document.activeElement).toBe(field("Name"));
    const message = document.getElementById(field("Name").getAttribute("aria-describedby") ?? "");
    expect(message?.textContent).toBe("A runtime needs a name.");
  });

  it("sends a valid form, handing over the form itself to read its fields from", async () => {
    const onSubmit = vi.fn();
    view = await mount(<CreateRuntime onSubmit={onSubmit} />);
    await type(field("Slug"), "codex-team");
    await click(submit());
    expect(onSubmit).toHaveBeenCalledOnce();
    const form = onSubmit.mock.calls[0]?.[0] as HTMLFormElement;
    expect(form.tagName).toBe("FORM");
    expect(new FormData(form).get("slug")).toBe("codex-team");
  });

  it("leaves the browser's own validation off, so the focus and the words are the form's", async () => {
    view = await mount(<CreateRuntime onSubmit={vi.fn()} />);
    expect(document.querySelector("form")?.noValidate).toBe(true);
  });
});

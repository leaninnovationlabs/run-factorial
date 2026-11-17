/**
 * The choices a reader has about a frame beside the page: the size of the conversation's text,
 * and for the small window its size, as one of three. The window's corner drags to any size;
 * this is the same thing for a keyboard.
 */

import { useState } from "react";
import { IconButton, Popup, RadioGroup } from "../ui";
import { useAssistant } from "./AssistantProvider";
import { MINI_PRESETS, type MiniPreset, presetOf, type TextStep } from "./frames";

const WINDOW_SIZES = [
  { value: "small", label: "Small" },
  { value: "medium", label: "Medium" },
  { value: "large", label: "Large" },
];

const TEXT_SIZES = [
  { value: "small", label: "Small" },
  { value: "default", label: "Default" },
  { value: "large", label: "Large" },
];

export function ViewOptions({ windowSize = false }: { windowSize?: boolean }) {
  const { prefs, setPrefs } = useAssistant();
  const [open, setOpen] = useState(false);
  const label = windowSize ? "Size and text" : "Text size";
  return (
    <Popup
      isOpen={open}
      onClose={() => setOpen(false)}
      label={label}
      placement="bottom-end"
      trigger={(props) => (
        <IconButton
          {...props}
          icon="sliders"
          label={label}
          spacing="compact"
          onClick={() => setOpen((value) => !value)}
        />
      )}
    >
      <div className="w-64 space-y-4 p-3">
        {windowSize && (
          <RadioGroup
            label="Window size"
            name="assistant-window"
            isInline
            options={WINDOW_SIZES}
            value={presetOf(prefs.mini) ?? ""}
            onChange={(value) => setPrefs({ mini: MINI_PRESETS[value as MiniPreset] })}
          />
        )}
        <RadioGroup
          label="Text size"
          name="assistant-text"
          isInline
          options={TEXT_SIZES}
          value={prefs.text}
          onChange={(value) => setPrefs({ text: value as TextStep })}
        />
        {windowSize && (
          <p className="text-body-sm text-subtlest">
            Or drag the window by its top-left corner to any size.
          </p>
        )}
      </div>
    </Popup>
  );
}

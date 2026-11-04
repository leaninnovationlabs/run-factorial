import { useEffect, useRef } from "react";
import type { useValidateProcess } from "../../api/queries";

/** Validates the text a moment after each edit, and says whether the verdict on screen is the
 *  one for the text on screen. */
export function useProcessValidation(
  validate: ReturnType<typeof useValidateProcess>,
  text: string,
) {
  const validated = useRef<string | null>(null);
  // `verdictFor` marks text whose verdict SUCCEEDED; the commit button keys on it, so a failed
  // validate request fails closed instead of leaving the button armed with no verdict.
  const verdictFor = useRef<string | null>(null);
  const { mutate: runValidate } = validate;
  useEffect(() => {
    if (!text.trim() || validated.current === text) return;
    const timer = setTimeout(() => {
      validated.current = text;
      runValidate(text, {
        onSuccess: () => {
          verdictFor.current = text;
        },
        // Checked, and the check failed: not "still checking", which would never end. The error
        // shows under the button, and the next edit checks again.
        onError: () => {},
      });
    }, 600);
    return () => clearTimeout(timer);
  }, [text, runValidate]);

  const verdict = validate.data;
  const validForText = Boolean(verdict?.valid) && verdictFor.current === text;
  const checkingValidity = validate.isPending || validated.current !== text;
  const checkFailed = !checkingValidity && validate.isError;
  return { verdict, validForText, checkingValidity, checkFailed };
}

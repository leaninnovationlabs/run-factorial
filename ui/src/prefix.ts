/** Where the app lives on its origin (tech-stack decision 74): Vite's `base`, without its slash.
 *  `orchestrator/settings.py`'s `APP_PREFIX` is the other copy. */
export const APP_PREFIX = import.meta.env.BASE_URL.replace(/\/+$/, "");

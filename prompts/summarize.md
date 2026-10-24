# Session Summary Task

Merge the existing Session summary, if present, with the supplied completed conversation turns.
Return only the new compact summary.

Preserve information that future turns may need:

- user preferences and constraints;
- durable facts supplied or confirmed by the user;
- unfinished tasks and open questions;
- key decisions and their reasons;
- important tool results, including identifiers, values, sources, and relevant errors.

Do not invent facts. Do not repeat transient wording, greetings, or superseded details. Clearly
distinguish confirmed facts from unresolved items.

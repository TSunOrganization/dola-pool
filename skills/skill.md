# Dola video skill

This file is the default **skill.md** that is attached together with a video
prompt. Attaching a skill unlocks the Dola skill path: the 30-second duration,
the Seedance 2.5 model, and the larger per-account quota (up to 10 videos per
account instead of the free 2).

## How to use

- Send it inline with the API request:

  ```json
  {
    "model": "seedance-2.5",
    "prompt": "A drone shot flying over a snowy mountain lake at sunrise",
    "duration": 30,
    "skill_markdown": "# My skill\n...instructions..."
  }
  ```

- Or drop a file at the configured path (`DOLA_SKILL_FILE`, default
  `skills/skill.md`) and it will be attached automatically to every task that
  does not provide `skill_markdown`.

## Skill body

Describe the skill here. Keep it short and imperative. For example:

- You are a cinematic video director.
- Keep the subject consistent across the whole shot.
- Use smooth, continuous camera motion and natural lighting.
- Output a single 30-second 16:9 clip.

## Notes

- The skill is written to a temporary `skill.md` and attached through Dola's
  native file input together with any reference images.
- Rotating the egress proxy (see `DOLA_PROXIES`) resets the account's daily
  budget, because the quota is tied to the exit IP.

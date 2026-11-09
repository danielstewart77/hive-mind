"""The installer's state between the page and the run.

Holds the answers (saved to `state/answers.json`, 0600, so a rerun starts
from them), the runner, and the one run thread. The page talks to this and
nothing else.
"""

from __future__ import annotations

import json
import threading

from installer import plan, secrets_store, steps
from installer.host import Host
from installer.paths import Home, write_private
from installer.runner import Locked, Runner

#: Answers that are secrets: never sent back to the page.
SECRET_KEYS = {"admin_password", "postgres_password", "proxy_key", "provider_api_key",
               "telegram_token", "discord_token"}
#: Which .env value stands for a secret answer, so a rerun may leave it blank.
KEPT_AS = {"admin_password": "HIVE_SEED_PASSWORD", "postgres_password": "POSTGRES_PASSWORD",
           "proxy_key": "MIND_PROXY_KEY", "telegram_token": "TELEGRAM_BOT_TOKEN",
           "discord_token": "DISCORD_BOT_TOKEN"}


class Installer:
    def __init__(self, home: Home, host: Host | None = None, step_list=None):
        self.home = home
        self.host = host or Host.detect()
        self.answers = self._load()
        self.runner = Runner(home, step_list if step_list is not None else steps.STEPS,
                             lambda sh: steps.Ctx(self, sh))
        self._thread: threading.Thread | None = None
        self.on_finished = None

    # ---- answers ----------------------------------------------------------
    def _load(self) -> dict:
        try:
            saved = json.loads(self.home.answers.read_text())
        except (FileNotFoundError, ValueError):
            saved = {}
        return {**plan.defaults(), **saved}

    def save(self) -> None:
        write_private(self.home.answers, json.dumps(self.answers, indent=2))

    def kept(self) -> set[str]:
        """Secret answers the hive already holds a value for."""
        env = secrets_store.current(self.home)
        out = {k for k, name in KEPT_AS.items() if env.get(name)}
        if self.answers.get("provider_api_key"):
            out.add("provider_api_key")
        return out

    def public_answers(self) -> dict:
        return {k: v for k, v in self.answers.items() if k not in SECRET_KEYS}

    def plan_view(self, proposed: dict | None = None) -> dict:
        answers = {**self.answers, **(proposed or {})}
        kept = self.kept()
        try:
            pieces = sorted(plan.installed(answers))
        except ValueError as exc:
            return {"error": str(exc)}
        return {
            "pieces": [{"name": p.name, "label": p.label, "required": p.required,
                        "modes": list(p.modes), "needs": list(p.needs),
                        "selected": p.name in pieces} for p in plan.PIECES.values()],
            "selected": pieces,
            "modes": {p: plan.mode(answers, p) for p in plan.PIECES if plan.PIECES[p].modes},
            "questions": [{"key": q.key, "label": q.label, "kind": q.kind,
                           "choices": list(q.choices), "default": q.default,
                           "optional": q.optional, "kept": q.key in kept}
                          for q in plan.questions(answers)],
            "answers": {k: v for k, v in answers.items() if k not in SECRET_KEYS},
            "repos": plan.repos(answers),
        }

    # ---- the run ----------------------------------------------------------
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, submitted: dict) -> list[str]:
        """Validate, save and start the run; the problems when it cannot."""
        if self.running():
            return ["the install is already running"]
        answers = {**self.answers}
        for key, value in submitted.items():
            if key in SECRET_KEYS and not str(value or "").strip():
                continue  # blank secret: keep what the hive has
            answers[key] = value
        problems = plan.validate(answers, self.kept())
        if problems:
            return problems
        answers["pieces"] = sorted(plan.installed(answers))
        answers.setdefault("mind_id", steps.new_mind_id())
        prior = self.answers.get("mind_name")
        if prior and prior != answers["mind_name"] and self.home.mind(prior).exists():
            return [f"this install's mind is {prior!r}; a second mind is not added by the installer"]
        self.answers = answers
        self.save()
        self._thread = threading.Thread(target=self._run, name="install-run", daemon=True)
        self._thread.start()
        return []

    def _run(self) -> None:
        try:
            ok = self.runner.run()
        except Locked as exc:
            self.runner.state["error"] = str(exc)
            ok = False
        if ok and self.on_finished:
            self.on_finished()

    def answer(self, key: str, value: str) -> list[str]:
        """An answer to the question a waiting step asked."""
        state = self.runner.snapshot()
        waiting = [s for s in state.get("steps", []) if s.get("status") == "waiting"]
        if not waiting:
            return ["no step is waiting for an answer"]
        question = waiting[0].get("question") or {}
        if question.get("key") != key:
            return [f"the waiting step asks for {question.get('key')!r}"]
        if question.get("choices") and value not in question["choices"]:
            return [f"choose one of: {', '.join(question['choices'])}"]
        self.answers[key] = value
        self.save()
        self.runner.answered.set()
        return []

    def state_view(self) -> dict:
        state = self.runner.snapshot()
        state["running"] = self.running()
        if self._thread is None:
            # What an earlier process left is history, not this run: a rerun
            # opens on the setup form.
            state["finished"] = False
            state["failed_step"] = None
            state["previous_run"] = True
        return state

from __future__ import annotations

import argparse
import json
import mimetypes
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

from werewolf_ai.engine import GameEngine
from werewolf_ai.evolution import EvolutionManager
from werewolf_ai.evaluator import evaluate_game
from werewolf_ai.memory_store import list_role_memory_versions, load_latest_promoted, save_review_memory_candidates
from werewolf_ai.reviewer import review_game_with_llm
from werewolf_ai.skill_evolution import normalize_evolution_mode
from werewolf_ai.service import (
    archive_game_payload,
    get_profiles,
    run_frozen_eval,
    run_ab_demo,
    run_demo_game,
    run_evolution_demo,
    serialize_event,
    serialize_game,
)
from werewolf_ai.models import ActionType, AgentDecision, Phase, PrivateObservation, Role, StrategyVersion, to_jsonable


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


@dataclass
class EvolutionJob:
    job_id: str
    params: dict[str, object]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "queued"
    result: dict | None = None
    error: dict | None = None
    sequence: int = 0
    history: list[dict[str, object]] = field(default_factory=list)
    subscribers: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event_name: str, data: dict[str, object], *, replay: bool = True) -> None:
        message = {
            "seq": self.sequence,
            "event": event_name,
            "data": to_jsonable({"job_id": self.job_id, **data}),
        }
        self.sequence += 1
        with self.lock:
            self.updated_at = time.time()
            if replay:
                self.history.append(message)
                self.history = self.history[-80:]
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                if event_name != "game_event":
                    try:
                        subscriber.put(message, timeout=1)
                    except queue.Full:
                        pass

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            for message in self.history:
                subscriber.put_nowait(message)
            self.subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "params": self.params,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "result": self.result,
                "error": self.error,
            }


class EvolutionJobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, EvolutionJob] = {}
        self.lock = threading.Lock()

    def start(
        self,
        *,
        rounds: int,
        games: int,
        base_version: str,
        llm_provider: str,
        llm_model: str | None,
        evolution_mode: str,
    ) -> EvolutionJob:
        evolution_mode = normalize_evolution_mode(evolution_mode)
        job_id = uuid.uuid4().hex[:12]
        job = EvolutionJob(
            job_id=job_id,
            params={
                "rounds": rounds,
                "games_per_round": games,
                "base_version": base_version,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "evolution_mode": evolution_mode,
            },
        )
        with self.lock:
            self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job, rounds, games, base_version, llm_provider, llm_model, evolution_mode),
            daemon=True,
            name=f"evolution-{job_id}",
        )
        thread.start()
        return job

    def get(self, job_id: str) -> EvolutionJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _run_job(
        self,
        job: EvolutionJob,
        rounds: int,
        games: int,
        base_version: str,
        llm_provider: str,
        llm_model: str | None,
        evolution_mode: str,
    ) -> None:
        job.status = "running"
        job.emit(
            "job_started",
            {
                "status": job.status,
                "rounds": rounds,
                "games_per_round": games,
                "base_version": base_version,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "evolution_mode": evolution_mode,
            },
        )
        try:
            manager = EvolutionManager(llm_provider=llm_provider, llm_model=llm_model, evolution_mode=evolution_mode)

            def progress(event_name: str, data: dict[str, object]) -> None:
                replay = event_name not in {"game_event", "game_completed"}
                job.emit(event_name, data, replay=replay)

            result = manager.run_evolution(
                rounds=rounds,
                games_per_round=games,
                base_version=base_version,
                progress_callback=progress,
            )
            job.result = to_jsonable({**result, "llm_provider": llm_provider, "llm_model": llm_model, "evolution_mode": manager.evolution_mode})
            job.status = "completed"
            job.emit("job_completed", {"status": job.status, "result": job.result})
        except Exception as exc:
            job.error = {"error": type(exc).__name__, "message": str(exc)}
            job.status = "failed"
            job.emit("job_failed", {"status": job.status, **job.error})


EVOLUTION_JOBS = EvolutionJobManager()


class FrozenEvalJobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, EvolutionJob] = {}
        self.lock = threading.Lock()

    def start(
        self,
        *,
        version: str,
        baseline_version: str,
        games: int,
        llm_provider: str,
        llm_model: str | None,
    ) -> EvolutionJob:
        job_id = uuid.uuid4().hex[:12]
        job = EvolutionJob(
            job_id=job_id,
            params={
                "version": version,
                "baseline_version": baseline_version,
                "games": games,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "mode": "frozen_eval",
            },
        )
        with self.lock:
            self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job, version, baseline_version, games, llm_provider, llm_model),
            daemon=True,
            name=f"frozen-eval-{job_id}",
        )
        thread.start()
        return job

    def get(self, job_id: str) -> EvolutionJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _run_job(
        self,
        job: EvolutionJob,
        version: str,
        baseline_version: str,
        games: int,
        llm_provider: str,
        llm_model: str | None,
    ) -> None:
        job.status = "running"
        job.emit(
            "job_started",
            {
                "status": job.status,
                "mode": "frozen_eval",
                "version": version,
                "baseline_version": baseline_version,
                "games": games,
                "total_games": games * 2,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
            },
        )
        try:
            def progress(event_name: str, data: dict[str, object]) -> None:
                replay = event_name not in {"game_event", "game_completed"}
                job.emit(event_name, data, replay=replay)

            result = run_frozen_eval(
                version=version,
                baseline_version=baseline_version,
                games=games,
                llm_provider=llm_provider,
                llm_model=llm_model,
                progress_callback=progress,
            )
            job.result = to_jsonable(result)
            job.status = "completed"
            job.emit("job_completed", {"status": job.status, "mode": "frozen_eval", "result": job.result})
        except Exception as exc:
            job.error = {"error": type(exc).__name__, "message": str(exc)}
            job.status = "failed"
            job.emit("job_failed", {"status": job.status, "mode": "frozen_eval", **job.error})


FROZEN_EVAL_JOBS = FrozenEvalJobManager()


@dataclass
class HumanPrompt:
    prompt_id: str
    observation: PrivateObservation
    profile: StrategyVersion
    created_at: float = field(default_factory=time.time)
    submitted_payload: dict[str, Any] | None = None
    event: threading.Event = field(default_factory=threading.Event)


class HumanGameSession:
    def __init__(
        self,
        *,
        session_id: str,
        human_player_id: int,
        emit: Any,
        timeout_seconds: int = 1800,
    ) -> None:
        self.session_id = session_id
        self.human_player_id = human_player_id
        self.emit = emit
        self.timeout_seconds = timeout_seconds
        self.lock = threading.Lock()
        self.prompts: dict[str, HumanPrompt] = {}
        self.pending_prompt_id: str | None = None

    def request_decision(self, observation: PrivateObservation, profile: StrategyVersion) -> dict[str, Any]:
        prompt_id = uuid.uuid4().hex[:12]
        prompt = HumanPrompt(prompt_id=prompt_id, observation=observation, profile=profile)
        with self.lock:
            self.prompts[prompt_id] = prompt
            self.pending_prompt_id = prompt_id
        self.emit("human_prompt", self._prompt_payload(prompt))
        if not prompt.event.wait(self.timeout_seconds):
            raise TimeoutError("Human decision timed out.")
        with self.lock:
            if self.pending_prompt_id == prompt_id:
                self.pending_prompt_id = None
        return prompt.submitted_payload or {}

    def submit_decision(self, prompt_id: str, payload: dict[str, Any]) -> None:
        with self.lock:
            prompt = self.prompts.get(prompt_id)
        if prompt is None:
            raise KeyError("Human prompt not found or already expired.")
        prompt.submitted_payload = dict(payload)
        prompt.event.set()

    def _prompt_payload(self, prompt: HumanPrompt) -> dict[str, Any]:
        observation = prompt.observation
        action_context = observation.private_knowledge.get("action_context", {})
        return {
            "session_id": self.session_id,
            "prompt_id": prompt.prompt_id,
            "human_player_id": self.human_player_id,
            "day": observation.day,
            "phase": observation.phase.value,
            "phase_zh": observation.phase.zh,
            "player": {
                "id": observation.self_id,
                "name": observation.self_name,
                "role": observation.self_role.value,
                "role_zh": observation.self_role.zh,
            },
            "known_teammates": _human_known_teammates(observation),
            "legal_actions": [action.value for action in observation.legal_actions],
            "target_options": _human_target_options(observation),
            "alive_players": to_jsonable(observation.alive_players),
            "private_knowledge": to_jsonable(observation.private_knowledge),
            "public_history_tail": to_jsonable(observation.public_history[-18:]),
            "belief_state": to_jsonable(observation.belief_state),
            "strategy_memory_tail": to_jsonable(observation.strategy_memory[-8:]),
            "action_context": to_jsonable(action_context),
            "game_rules": _human_rule_summary(observation),
            "created_at": prompt.created_at,
        }


class HumanSessionRegistry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, HumanGameSession] = {}

    def create(self, *, human_player_id: int, emit: Any) -> HumanGameSession:
        session_id = uuid.uuid4().hex[:12]
        session = HumanGameSession(session_id=session_id, human_player_id=human_player_id, emit=emit)
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> HumanGameSession | None:
        with self.lock:
            return self.sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        with self.lock:
            self.sessions.pop(session_id, None)


class HumanDecisionClient:
    def __init__(self, session: HumanGameSession) -> None:
        self.session = session
        self.chat_client = SimpleNamespace(config=SimpleNamespace(provider="human", model="typed-input"))

    def decide(self, observation: PrivateObservation, profile: StrategyVersion) -> AgentDecision:
        start = time.perf_counter()
        payload = self.session.request_decision(observation, profile)
        decision = _human_payload_to_decision(payload, observation)
        decision.metadata.update(
            {
                "decision_backend": "human",
                "human_session_id": self.session.session_id,
                "human_player_id": self.session.human_player_id,
                "human_decision_time_ms": int((time.perf_counter() - start) * 1000),
            }
        )
        return decision


HUMAN_SESSIONS = HumanSessionRegistry()


class WerewolfRequestHandler(BaseHTTPRequestHandler):
    server_version = "WerewolfAI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._json({"ok": True, "service": "AI Werewolf B+C demo"})
            elif parsed.path == "/api/versions":
                self._json(_available_versions())
            elif parsed.path == "/api/game/stream":
                self._stream_game(parse_qs(parsed.query))
            elif parsed.path == "/api/game":
                query = parse_qs(parsed.query)
                seed = _optional_int(query.get("seed", [None])[0])
                version = query.get("version", ["initial"])[0]
                llm_provider = query.get("llm_provider", [query.get("provider", ["ark"])[0]])[0]
                llm_model = query.get("llm_model", [query.get("model", [None])[0]])[0]
                self._json(
                    run_demo_game(
                        seed=seed,
                        version=version,
                        llm_provider=llm_provider,
                        llm_model=llm_model,
                    )
                )
            elif parsed.path == "/api/evolution/start":
                query = parse_qs(parsed.query)
                rounds = _bounded_int(query.get("rounds", ["2"])[0], default=2, minimum=1, maximum=4)
                games = _bounded_int(query.get("games", ["20"])[0], default=20, minimum=1, maximum=50)
                base_version = query.get("base_version", ["latest"])[0] or "latest"
                llm_provider = query.get("llm_provider", [query.get("provider", ["ark"])[0]])[0]
                llm_model = query.get("llm_model", [query.get("model", [None])[0]])[0]
                evolution_mode = normalize_evolution_mode(query.get("evolution_mode", ["workflow"])[0])
                job = EVOLUTION_JOBS.start(
                    rounds=rounds,
                    games=games,
                    base_version=base_version,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                    evolution_mode=evolution_mode,
                )
                self._json(job.snapshot())
            elif parsed.path == "/api/evolution/status":
                query = parse_qs(parsed.query)
                job = EVOLUTION_JOBS.get(query.get("job_id", [""])[0])
                if job is None:
                    self._json({"error": "not_found", "message": "Evolution job not found"}, status=404)
                else:
                    self._json(job.snapshot())
            elif parsed.path == "/api/evolution/events":
                self._stream_evolution_job(parse_qs(parsed.query))
            elif parsed.path == "/api/evolution":
                query = parse_qs(parsed.query)
                rounds = _bounded_int(query.get("rounds", ["2"])[0], default=2, minimum=1, maximum=4)
                games = _bounded_int(query.get("games", ["20"])[0], default=20, minimum=1, maximum=50)
                base_version = query.get("base_version", ["latest"])[0] or "latest"
                llm_provider = query.get("llm_provider", [query.get("provider", ["ark"])[0]])[0]
                llm_model = query.get("llm_model", [query.get("model", [None])[0]])[0]
                evolution_mode = normalize_evolution_mode(query.get("evolution_mode", ["workflow"])[0])
                self._json(
                    run_evolution_demo(
                        rounds=rounds,
                        games_per_round=games,
                        base_version=base_version,
                        llm_provider=llm_provider,
                        llm_model=llm_model,
                        evolution_mode=evolution_mode,
                    )
                )
            elif parsed.path == "/api/frozen-eval/start":
                query = parse_qs(parsed.query)
                version, baseline_version, games, llm_provider, llm_model = _frozen_eval_params(query)
                job = FROZEN_EVAL_JOBS.start(
                    version=version,
                    baseline_version=baseline_version,
                    games=games,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                )
                self._json(job.snapshot())
            elif parsed.path == "/api/frozen-eval/status":
                query = parse_qs(parsed.query)
                job = FROZEN_EVAL_JOBS.get(query.get("job_id", [""])[0])
                if job is None:
                    self._json({"error": "not_found", "message": "Frozen eval job not found"}, status=404)
                else:
                    self._json(job.snapshot())
            elif parsed.path == "/api/frozen-eval/events":
                self._stream_frozen_eval_job(parse_qs(parsed.query))
            elif parsed.path == "/api/frozen-eval":
                query = parse_qs(parsed.query)
                version, baseline_version, games, llm_provider, llm_model = _frozen_eval_params(query)
                self._json(
                    run_frozen_eval(
                        version=version,
                        baseline_version=baseline_version,
                        games=games,
                        llm_provider=llm_provider,
                        llm_model=llm_model,
                    )
                )
            elif parsed.path == "/api/ab":
                query = parse_qs(parsed.query)
                games = _bounded_int(query.get("games", ["20"])[0], default=20, minimum=1, maximum=50)
                llm_provider = query.get("llm_provider", [query.get("provider", ["ark"])[0]])[0]
                llm_model = query.get("llm_model", [query.get("model", [None])[0]])[0]
                self._json(run_ab_demo(games=games, llm_provider=llm_provider, llm_model=llm_model))
            else:
                self._static(parsed.path)
        except Exception as exc:  # pragma: no cover - defensive API boundary.
            self._json({"error": type(exc).__name__, "message": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/human/decision":
                payload = self._read_json_body()
                session_id = str(payload.get("session_id") or "")
                prompt_id = str(payload.get("prompt_id") or "")
                session = HUMAN_SESSIONS.get(session_id)
                if session is None:
                    self._json({"error": "not_found", "message": "Human game session not found."}, status=404)
                    return
                session.submit_decision(prompt_id, payload)
                self._json({"ok": True, "session_id": session_id, "prompt_id": prompt_id})
            else:
                self._json({"error": "not_found", "message": f"No route for {parsed.path}"}, status=404)
        except Exception as exc:  # pragma: no cover - defensive API boundary.
            self._json({"error": type(exc).__name__, "message": str(exc)}, status=400)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _static(self, path: str) -> None:
        if path in {"", "/"}:
            file_path = STATIC_DIR / "index.html"
        else:
            relative = path.lstrip("/")
            if relative.startswith("static/"):
                relative = relative[len("static/") :]
            file_path = STATIC_DIR / relative

        resolved = file_path.resolve()
        if not str(resolved).startswith(str(STATIC_DIR.resolve())) or not resolved.exists() or not resolved.is_file():
            self._json({"error": "not_found", "message": f"No route for {path}"}, status=404)
            return

        content = resolved.read_bytes()
        mime, _ = mimetypes.guess_type(str(resolved))
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object.")
        return data

    def _stream_game(self, query: dict[str, list[str]]) -> None:
        seed = _optional_int(query.get("seed", [None])[0])
        version = query.get("version", ["initial"])[0]
        llm_provider = query.get("llm_provider", [query.get("provider", ["ark"])[0]])[0]
        llm_model = query.get("llm_model", [query.get("model", [None])[0]])[0]
        mode = query.get("mode", ["observer"])[0]
        human_mode = mode == "human"
        human_player_id = _bounded_int(query.get("human_player_id", ["1"])[0], default=1, minimum=1, maximum=9)
        profiles = get_profiles(version)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        human_session: HumanGameSession | None = None

        def on_event(event, engine: GameEngine) -> None:
            if human_mode and not _event_visible_to_human(event, human_player_id):
                return
            reveal_private = (not human_mode) or (
                event.actor_id == human_player_id and not event.public_payload.get("visible_to_players", True)
            )
            self._sse_send(
                "game_event",
                {
                    "event": _serialize_event_for_live(event, reveal_private=reveal_private),
                    "summary": _live_summary(engine, human_player_id=human_player_id if human_mode else None),
                    "server_time": time.time(),
                },
            )

        try:
            if human_mode:
                human_session = HUMAN_SESSIONS.create(human_player_id=human_player_id, emit=self._sse_send)
            engine = GameEngine(
                seed=seed,
                profiles=profiles,
                version_id=version,
                llm_provider=llm_provider,
                llm_model=llm_model,
                event_callback=on_event,
            )
            if human_mode and human_session is not None:
                engine.agents[human_player_id].llm_decider = HumanDecisionClient(human_session)
            known_role_ids = _known_role_ids_for_human(engine, human_player_id) if human_mode else None
            self._sse_send(
                "game_start",
                {
                    "game_id": engine.game_id,
                    "seed": engine.seed,
                    "version_id": version,
                    "mode": "human" if human_mode else "observer",
                    "agent_backend": "human+llm" if human_mode else "llm",
                    "human_session_id": human_session.session_id if human_session else None,
                    "human_player_id": human_player_id if human_mode else None,
                    "llm_provider": llm_provider,
                    "llm_model": llm_model,
                    "players": [
                        _serialize_player(
                            player,
                            human_player_id=human_player_id if human_mode else None,
                            known_role_ids=known_role_ids,
                        )
                        for player in engine.players
                    ],
                    "strategy_versions": {role.value: to_jsonable(profile) for role, profile in profiles.items()},
                },
            )
            result = engine.run()
            report = evaluate_game(result)
            llm_review = review_game_with_llm(
                result,
                report,
                llm_provider=llm_provider,
                llm_model=llm_model,
                enabled=True,
            )
            if llm_review is not None:
                bank_item_ids = save_review_memory_candidates(llm_review, version_id=version, game_id=result.game_id)
                if bank_item_ids:
                    llm_review["memory_bank_item_ids"] = bank_item_ids
            final_payload = {
                "game": serialize_game(result),
                "report": to_jsonable(report),
                "llm_review": to_jsonable(llm_review),
                "strategy_versions": {role.value: to_jsonable(profile) for role, profile in profiles.items()},
                "private_observation_proof": result.observation_samples[:80],
                "agent_backend": "human+llm" if human_mode else "llm",
                "mode": "human" if human_mode else "observer",
                "human_player_id": human_player_id if human_mode else None,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
            }
            final_payload["archive_path"] = archive_game_payload(final_payload)
            self._sse_send("game_final", final_payload)
            self._sse_send("done", {"ok": True})
        except Exception as exc:
            self._sse_send("game_error", {"error": type(exc).__name__, "message": str(exc)})
            self._sse_send("done", {"ok": False})
        finally:
            if human_session is not None:
                HUMAN_SESSIONS.remove(human_session.session_id)

    def _stream_evolution_job(self, query: dict[str, list[str]]) -> None:
        job_id = query.get("job_id", [""])[0]
        job = EVOLUTION_JOBS.get(job_id)
        if job is None:
            self._json({"error": "not_found", "message": "Evolution job not found"}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        subscriber = job.subscribe()
        try:
            self._sse_send("job_snapshot", job.snapshot())
            while True:
                try:
                    message = subscriber.get(timeout=15)
                except queue.Empty:
                    self._sse_send("heartbeat", job.snapshot())
                    if job.status in {"completed", "failed"}:
                        break
                    continue
                self._sse_send(str(message["event"]), message["data"])
                if message["event"] in {"job_completed", "job_failed"}:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            job.unsubscribe(subscriber)

    def _stream_frozen_eval_job(self, query: dict[str, list[str]]) -> None:
        job_id = query.get("job_id", [""])[0]
        job = FROZEN_EVAL_JOBS.get(job_id)
        if job is None:
            self._json({"error": "not_found", "message": "Frozen eval job not found"}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        subscriber = job.subscribe()
        try:
            self._sse_send("job_snapshot", job.snapshot())
            while True:
                try:
                    message = subscriber.get(timeout=15)
                except queue.Empty:
                    self._sse_send("heartbeat", job.snapshot())
                    if job.status in {"completed", "failed"}:
                        break
                    continue
                self._sse_send(str(message["event"]), message["data"])
                if message["event"] in {"job_completed", "job_failed"}:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            job.unsubscribe(subscriber)

    def _sse_send(self, event_name: str, data: object) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        frame = f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(frame)
        self.wfile.flush()


def _optional_int(value: str | None) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _frozen_eval_params(query: dict[str, list[str]]) -> tuple[str, str, int, str, str | None]:
    version = query.get("version", ["latest"])[0] or "latest"
    baseline_version = (
        query.get("baseline_version", [query.get("baseline", [query.get("base_version", ["initial"])[0]])[0]])[0]
        or "initial"
    )
    games = _bounded_int(query.get("games", ["20"])[0], default=20, minimum=1, maximum=50)
    llm_provider = query.get("llm_provider", [query.get("provider", ["ark"])[0]])[0]
    llm_model = query.get("llm_model", [query.get("model", [None])[0]])[0]
    return version, baseline_version, games, llm_provider, llm_model


def _available_versions() -> dict[str, Any]:
    latest = load_latest_promoted()
    persisted = list_role_memory_versions()
    seen: set[str] = set()
    versions: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        version_id = str(item.get("version_id") or "").strip()
        if not version_id or version_id in seen:
            return
        seen.add(version_id)
        versions.append(item)

    latest_version = str((latest or {}).get("version_id") or "")
    add(
        {
            "version_id": "latest",
            "label": f"latest promoted ({latest_version})" if latest_version else "latest promoted",
            "kind": "alias",
            "target_version_id": latest_version or None,
            "promoted": True,
        }
    )
    add({"version_id": "initial", "label": "initial", "kind": "builtin", "promoted": True})
    add({"version_id": "evolved", "label": "evolved legacy", "kind": "builtin", "promoted": True})

    for item in persisted:
        label = str(item.get("version_id") or "")
        suffixes: list[str] = []
        if latest_version and label == latest_version:
            suffixes.append("current")
        if item.get("promoted"):
            suffixes.append("promoted")
        overall = _optional_float(item.get("overall"))
        if overall is not None:
            suffixes.append(f"overall {overall:.2f}")
        add(
            {
                **item,
                "kind": "memory",
                "label": f"{label} ({', '.join(suffixes)})" if suffixes else label,
            }
        )

    return {
        "latest": latest,
        "versions": versions,
    }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _serialize_player(
    player,
    *,
    human_player_id: int | None = None,
    known_role_ids: set[int] | None = None,
) -> dict:
    reveal = human_player_id is None or player.id in (known_role_ids or {human_player_id})
    data = {
        **player.public_view(False),
        "agent_version": player.agent_version,
    }
    if reveal:
        data.update(
            {
                "role": player.role.value,
                "role_zh": player.role.zh,
                "faction": player.faction.value,
                "faction_zh": player.faction.zh,
            }
        )
    else:
        data.update({"role": "unknown", "role_zh": "未知", "faction": "unknown", "faction_zh": "未知"})
    if human_player_id is not None:
        data["is_human"] = player.id == human_player_id
    return data


def _serialize_event_for_live(event, *, reveal_private: bool) -> dict:
    if reveal_private:
        return serialize_event(event)
    data = event.public_view()
    data["private_payload"] = {}
    data["observation_proof"] = None
    data["observation_snapshot"] = None
    data["decision"] = None
    return data


def _event_visible_to_human(event, human_player_id: int) -> bool:
    if event.public_payload.get("visible_to_players", True):
        return True
    return bool(event.actor_id == human_player_id)


def _live_summary(engine: GameEngine, *, human_player_id: int | None = None) -> dict:
    alive = [player for player in engine.players if player.alive]
    wolves_alive = [player for player in alive if player.role.value == "werewolf"]
    civilians_alive = [player for player in alive if player.role.value == "villager"]
    gods_alive = [player for player in alive if player.role.value in {"seer", "witch", "hunter"}]
    reveal_counts = human_player_id is None
    known_role_ids = _known_role_ids_for_human(engine, human_player_id) if human_player_id is not None else None
    return {
        "day": engine.day,
        "alive_count": len(alive),
        "dead_count": len(engine.players) - len(alive),
        "wolves_alive": len(wolves_alive) if reveal_counts else None,
        "civilians_alive": len(civilians_alive) if reveal_counts else None,
        "gods_alive": len(gods_alive) if reveal_counts else None,
        "winner": engine.winner.value if engine.winner else None,
        "win_reason": engine.win_reason,
        "players": [
            _serialize_player(player, human_player_id=human_player_id, known_role_ids=known_role_ids)
            for player in engine.players
        ],
    }


def _known_role_ids_for_human(engine: GameEngine, human_player_id: int) -> set[int]:
    human_player = next(player for player in engine.players if player.id == human_player_id)
    known = {human_player_id}
    if human_player.role == Role.WEREWOLF:
        known.update(player.id for player in engine.players if player.role == Role.WEREWOLF)
    return known


def _human_rule_summary(observation: PrivateObservation) -> dict[str, Any]:
    rules = observation.game_rules or {}
    return {
        "ruleset_name": rules.get("ruleset_name"),
        "player_count": rules.get("player_count"),
        "role_distribution": rules.get("role_distribution"),
        "disabled_mechanics": rules.get("disabled_mechanics"),
        "win_conditions": rules.get("win_conditions"),
        "phase_order": rules.get("phase_order"),
    }


def _human_known_teammates(observation: PrivateObservation) -> list[dict[str, Any]]:
    if observation.self_role != Role.WEREWOLF:
        return []
    pack_ids = set(observation.private_knowledge.get("pack_ids") or [])
    pack_names = observation.private_knowledge.get("pack_names") or []
    by_id: dict[int, dict[str, Any]] = {}
    for player in observation.alive_players:
        player_id = int(player.get("id"))
        if player_id in pack_ids and player_id != observation.self_id:
            by_id[player_id] = {"id": player_id, "name": player.get("name") or f"P{player_id}", "role": "werewolf"}
    for player_id, name in zip(sorted(pack_ids), pack_names):
        if player_id != observation.self_id:
            by_id.setdefault(int(player_id), {"id": int(player_id), "name": name or f"P{player_id}", "role": "werewolf"})
    return [by_id[player_id] for player_id in sorted(by_id)]


def _human_target_options(observation: PrivateObservation) -> list[dict[str, Any]]:
    legal = set(observation.legal_actions)
    context = observation.private_knowledge.get("action_context", {})
    tie_pk = context.get("tie_pk", {}) if isinstance(context, dict) else {}
    allowed_vote_targets = set(tie_pk.get("allowed_vote_targets") or [])
    attacked_tonight = observation.private_knowledge.get("attacked_tonight")
    pack_ids = set(observation.private_knowledge.get("pack_ids") or [])
    options: list[dict[str, Any]] = []
    for player in observation.alive_players:
        player_id = int(player.get("id"))
        if player_id == observation.self_id and not (ActionType.SAVE in legal and attacked_tonight == player_id):
            continue
        allowed_actions: list[str] = []
        if ActionType.KILL in legal and player_id not in pack_ids:
            allowed_actions.append(ActionType.KILL.value)
        if ActionType.INSPECT in legal:
            allowed_actions.append(ActionType.INSPECT.value)
        if ActionType.POISON in legal:
            allowed_actions.append(ActionType.POISON.value)
        if ActionType.SHOOT in legal:
            allowed_actions.append(ActionType.SHOOT.value)
        if ActionType.VOTE in legal and (not allowed_vote_targets or player_id in allowed_vote_targets):
            allowed_actions.append(ActionType.VOTE.value)
        if ActionType.SAVE in legal and attacked_tonight == player_id:
            allowed_actions.append(ActionType.SAVE.value)
        if allowed_actions:
            options.append(
                {
                    "id": player_id,
                    "name": player.get("name") or f"P{player_id}",
                    "allowed_actions": allowed_actions,
                }
            )
    return options


def _human_payload_to_decision(payload: dict[str, Any], observation: PrivateObservation) -> AgentDecision:
    action = _parse_action(payload.get("action"))
    legal = set(observation.legal_actions)
    if action not in legal:
        raise ValueError(f"Action {action.value} is not legal in current phase.")
    target_id = _optional_target(payload.get("target_id"))
    if action in {ActionType.PASS, ActionType.SPEAK}:
        target_id = None
    else:
        _validate_human_target(action, target_id, observation)
    speech = str(payload.get("speech") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if action == ActionType.SPEAK and not speech:
        speech = "我暂时没有更多信息。"
    if not reason:
        reason = "真人玩家手动提交。"
    confidence = _parse_confidence(payload.get("confidence"))
    return AgentDecision(
        actor_id=observation.self_id,
        role=observation.self_role,
        action=action,
        target_id=target_id,
        speech=speech,
        reason=reason,
        confidence=confidence,
    )


def _validate_human_target(action: ActionType, target_id: int | None, observation: PrivateObservation) -> None:
    if target_id is None:
        raise ValueError(f"Action {action.value} requires a target.")
    if target_id == observation.self_id and action != ActionType.SAVE:
        raise ValueError("You cannot target yourself with this action.")
    alive_ids = {int(player["id"]) for player in observation.alive_players}
    if target_id not in alive_ids:
        raise ValueError(f"Target P{target_id} is not alive.")
    if action == ActionType.KILL and target_id in set(observation.private_knowledge.get("pack_ids") or []):
        raise ValueError("Werewolves cannot kill a wolf teammate at night.")
    if action == ActionType.SAVE and target_id != observation.private_knowledge.get("attacked_tonight"):
        raise ValueError("Save action must target attacked_tonight.")
    if action == ActionType.VOTE:
        tie_pk = observation.private_knowledge.get("action_context", {}).get("tie_pk", {})
        allowed = set(tie_pk.get("allowed_vote_targets") or [])
        if allowed and target_id not in allowed:
            raise ValueError(f"PK revote target must be one of {sorted(allowed)}.")


def _parse_action(value: object) -> ActionType:
    try:
        return ActionType(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"Unknown action: {value}") from exc


def _optional_target(value: object) -> int | None:
    if value in {None, "", "null", "none", "None"}:
        return None
    return int(value)


def _parse_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.75
    if confidence > 1:
        confidence = confidence / 100
    return max(0.0, min(1.0, confidence))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AI Werewolf observer UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), WerewolfRequestHandler)
    print(f"AI Werewolf demo running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

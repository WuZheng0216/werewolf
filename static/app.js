const state = {
  game: null,
  report: null,
  strategies: null,
  filter: "all",
  currentIndex: 0,
  playing: false,
  timer: null,
  source: null,
  evolutionSource: null,
  evolution: null,
  streaming: false,
  archivePath: null,
  llmReview: null,
  skillUsageSummary: null,
  liveStatus: "",
  human: {
    mode: "observer",
    sessionId: null,
    playerId: null,
    pendingPrompt: null,
    submitting: false,
    finalRevealed: false,
  },
};

const scoreLabels = {
  overall: "Overall",
  speech_quality: "Speech",
  vote_quality: "Vote",
  skill_quality: "Skill",
  wolf_deception_quality: "Wolf Deception",
  wolf_deception_diversity: "Deception Variety",
  social_influence_quality: "Social Influence",
  team_contribution: "Team",
  mistake_penalty: "Mistakes",
};

const roleClass = {
  werewolf: "werewolf",
  seer: "seer",
  witch: "witch",
  hunter: "hunter",
  villager: "villager",
};

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("runGameBtn").addEventListener("click", () => loadGame());
  document.getElementById("runEvolutionBtn").addEventListener("click", () => runEvolution());
  document.getElementById("runFrozenEvalBtn").addEventListener("click", () => runFrozenEval());
  document.getElementById("runAbBtn").addEventListener("click", () => runAb());
  document.getElementById("playModeSelect").addEventListener("change", () => {
    state.human.mode = document.getElementById("playModeSelect").value;
    renderHumanPanel();
    renderIoDebug();
  });
  document.getElementById("playPauseBtn").addEventListener("click", () => togglePlayback());
  document.getElementById("prevEventBtn").addEventListener("click", () => stepEvent(-1));
  document.getElementById("nextEventBtn").addEventListener("click", () => stepEvent(1));
  document.getElementById("eventSlider").addEventListener("input", (event) => {
    pausePlayback();
    setCurrentIndex(Number(event.target.value));
  });
  document.getElementById("speedSelect").addEventListener("change", () => {
    if (state.playing) {
      pausePlayback();
      startPlayback();
    }
  });
  document.getElementById("ioPlayerSelect").addEventListener("change", () => renderIoDebug());
  document.querySelectorAll(".segmented button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".segmented button").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.filter = button.dataset.filter;
      renderTimeline();
    });
  });
  setLoading("timeline", "Choose a model and click New Game.");
  setLoading("ioDebug", "Agent I/O will appear after decisions stream in.");
  renderHumanPanel();
  document.getElementById("summaryBand").innerHTML = "";
  loadVersionOptions();
});

function loadGame() {
  pausePlayback();
  closeEvolutionStream();
  if (state.source) {
    state.source.close();
    state.source = null;
  }
  state.streaming = true;
  state.game = null;
  state.report = null;
  state.archivePath = null;
  state.llmReview = null;
  state.skillUsageSummary = null;
  state.liveStatus = "Connecting live stream...";
  state.human = {
    mode: document.getElementById("playModeSelect").value || "observer",
    sessionId: null,
    playerId: Number(document.getElementById("humanSeatSelect").value || 1),
    pendingPrompt: null,
    submitting: false,
    finalRevealed: false,
  };
  const version = document.getElementById("versionSelect").value;
  const [llmProvider, llmModel] = document.getElementById("modelSelect").value.split("|");
  const seed = document.getElementById("seedInput").value.trim();
  setLoading("timeline", "Streaming game...");
  setLoading("ioDebug", state.human.mode === "human" ? "Human mode hides Agent I/O until the game ends." : "Waiting for agent decisions...");
  renderHumanPanel();
  document.getElementById("coreEvent").textContent = "Connecting live stream...";
  const url = new URL("/api/game/stream", window.location.origin);
  url.searchParams.set("version", version);
  url.searchParams.set("llm_provider", llmProvider);
  if (llmModel) url.searchParams.set("llm_model", llmModel);
  if (seed) url.searchParams.set("seed", seed);
  url.searchParams.set("mode", state.human.mode);
  if (state.human.mode === "human") url.searchParams.set("human_player_id", state.human.playerId);

  const source = new EventSource(url.toString());
  state.source = source;

  source.addEventListener("game_start", (message) => {
    applyGameStart(JSON.parse(message.data));
  });

  source.addEventListener("human_prompt", (message) => {
    applyHumanPrompt(JSON.parse(message.data));
  });

  source.addEventListener("game_event", (message) => {
    applyGameEvent(JSON.parse(message.data));
  });

  source.addEventListener("game_final", (message) => {
    applyGameFinal(JSON.parse(message.data));
  });

  source.addEventListener("game_error", (message) => {
    const data = JSON.parse(message.data);
    state.streaming = false;
    state.human.pendingPrompt = null;
    setLoading("timeline", `${data.error}: ${data.message}`);
    setLoading("ioDebug", "Game stream stopped before a valid game was produced.");
    document.getElementById("coreEvent").textContent = data.message;
    renderHumanPanel();
    source.close();
    if (state.source === source) state.source = null;
  });

  source.addEventListener("done", () => {
    state.streaming = false;
    source.close();
    if (state.source === source) state.source = null;
  });

  source.onerror = () => {
    state.streaming = false;
    source.close();
    if (state.source === source) state.source = null;
    if (!state.game) {
      setLoading("timeline", "Stream failed. Please retry.");
    }
  };
}

function applyGameStart(data) {
  state.streaming = true;
  state.liveStatus = "Game started. Waiting for the first phase...";
  state.human.mode = data.mode || state.human.mode || "observer";
  state.human.sessionId = data.human_session_id || null;
  state.human.playerId = data.human_player_id || state.human.playerId;
  state.human.pendingPrompt = null;
  state.human.finalRevealed = false;
  state.game = {
    game_id: data.game_id,
    seed: data.seed,
    version_id: data.version_id,
    winner: null,
    winner_zh: "running",
    win_reason: "",
    day: 0,
    players: data.players || [],
    events: [],
    summary: {
      alive_count: (data.players || []).length,
      dead_count: 0,
      wolves_alive: data.mode === "human" ? null : (data.players || []).filter((player) => player.role === "werewolf").length,
      villagers_alive: data.mode === "human" ? null : (data.players || []).filter((player) => player.role !== "werewolf").length,
      civilians_alive: data.mode === "human" ? null : (data.players || []).filter((player) => player.role === "villager").length,
      gods_alive: data.mode === "human" ? null : (data.players || []).filter((player) => ["seer", "witch", "hunter"].includes(player.role)).length,
    },
  };
  state.report = null;
  state.archivePath = null;
  state.llmReview = null;
  state.skillUsageSummary = null;
  state.strategies = data.strategy_versions || null;
  state.currentIndex = 0;
  renderAll();
  renderHumanPanel();
}

function applyHumanPrompt(data) {
  state.human.mode = "human";
  state.human.sessionId = data.session_id;
  state.human.playerId = data.human_player_id;
  state.human.pendingPrompt = data;
  state.human.submitting = false;
  state.liveStatus = `轮到你行动：D${data.day} ${data.phase_zh || data.phase}`;
  renderHumanPanel();
}

function applyGameEvent(data) {
  if (!state.game) return;
  state.game.events.push(data.event);
  if (data.event?.event_type === "phase_status") {
    state.liveStatus = data.event.public_text || state.liveStatus;
  }
  state.game.players = data.summary?.players || state.game.players;
  state.game.day = data.summary?.day ?? state.game.day;
  state.game.summary = {
    ...state.game.summary,
    ...(data.summary || {}),
    villagers_alive:
      data.summary && data.summary.wolves_alive !== null && data.summary.wolves_alive !== undefined
        ? data.summary.alive_count - data.summary.wolves_alive
        : state.game.summary.villagers_alive,
  };
  if (data.summary?.winner) {
    state.game.winner = data.summary.winner;
    state.game.win_reason = data.summary.win_reason;
  }
  state.currentIndex = state.game.events.length - 1;
  renderSummary();
  renderStage();
  renderTimeline();
  renderIoPlayerSelect();
  renderIoDebug();
  renderHumanPanel();
}

function applyGameFinal(data) {
  state.game = data.game;
  state.report = data.report;
  state.llmReview = data.llm_review || null;
  state.strategies = data.strategy_versions || state.strategies;
  state.archivePath = data.archive_path || null;
  state.skillUsageSummary = data.skill_usage_summary || null;
  state.liveStatus = "Game finished. Full replay is available.";
  state.human.pendingPrompt = null;
  state.human.finalRevealed = true;
  state.currentIndex = Math.max(0, state.game.events.length - 1);
  state.streaming = false;
  renderAll();
  renderHumanPanel();
}

async function runAb() {
  document.getElementById("abPill").textContent = "Running";
  setLoading("abResult", "Running AB matches...");
  document.getElementById("leaderboard").innerHTML = "";
  try {
    const data = await fetchJson(buildRunUrl("/api/ab", { games: boundedInput("abGamesInput", 1, 50, 6) }));
    renderAb(data);
  } catch (error) {
    document.getElementById("abPill").textContent = "Failed";
    setLoading("abResult", error.message || String(error));
  }
}

async function runFrozenEval() {
  closeEvolutionStream();
  const version = document.getElementById("versionSelect").value || "latest";
  const baselineVersion = document.getElementById("baseMemorySelect").value || "initial";
  const games = boundedInput("abGamesInput", 1, 50, 6);
  document.getElementById("abPill").textContent = "Starting";
  setLoading("abResult", `Starting frozen eval: ${version} vs ${baselineVersion}...`);
  document.getElementById("leaderboard").innerHTML = "";
  try {
    const job = await fetchJson(
      buildRunUrl("/api/frozen-eval/start", {
        version,
        baseline_version: baselineVersion,
        games,
      }),
    );
    state.evolution = {
      jobId: job.job_id,
      status: job.status,
      rounds: 0,
      gamesPerRound: games,
      evolutionMode: "frozen_eval",
      requestedBaseVersion: baselineVersion,
      baseVersion: baselineVersion,
      candidateVersion: version,
      totalGames: games * 2,
      completedGames: 0,
      failedGames: 0,
      currentVersion: version,
      currentGameIndex: 0,
      currentSide: "candidate",
      logs: [`Frozen eval ${job.job_id} created: ${version} vs ${baselineVersion}.`],
      versions: [],
      finalResult: null,
    };
    renderEvolutionProgress();
    subscribeFrozenEval(job.job_id);
  } catch (error) {
    document.getElementById("abPill").textContent = "Failed";
    setLoading("abResult", error.message || String(error));
  }
}

async function runEvolution() {
  closeEvolutionStream();
  document.getElementById("abPill").textContent = "Starting";
  setLoading("abResult", "Starting background evolution job...");
  document.getElementById("leaderboard").innerHTML = "";
  try {
    const rounds = boundedInput("evoRoundsInput", 1, 4, 1);
    const games = boundedInput("evoGamesInput", 1, 50, 3);
    const baseVersion = document.getElementById("baseMemorySelect").value || "latest";
    const evolutionMode = document.getElementById("evolutionModeSelect").value || "workflow";
    const job = await fetchJson(buildRunUrl("/api/evolution/start", { rounds, games, base_version: baseVersion, evolution_mode: evolutionMode }));
    state.evolution = {
      jobId: job.job_id,
      status: job.status,
      rounds,
      gamesPerRound: games,
      evolutionMode: job.params?.evolution_mode || evolutionMode,
      requestedBaseVersion: baseVersion,
      baseVersion: job.params?.base_version || baseVersion,
      totalGames: (rounds + 1) * games,
      completedGames: 0,
      failedGames: 0,
      currentVersion: baseVersion,
      currentGameIndex: 0,
      logs: [`Job ${job.job_id} created. Mode: ${job.params?.evolution_mode || evolutionMode}.`],
      versions: [],
      finalResult: null,
    };
    renderEvolutionProgress();
    subscribeEvolution(job.job_id);
  } catch (error) {
    document.getElementById("abPill").textContent = "Failed";
    setLoading("abResult", error.message || String(error));
  }
}

function subscribeEvolution(jobId) {
  const url = new URL("/api/evolution/events", window.location.origin);
  url.searchParams.set("job_id", jobId);
  const source = new EventSource(url.toString());
  state.evolutionSource = source;

  source.addEventListener("job_snapshot", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.status = data.status || state.evolution.status;
      state.evolution.baseVersion = data.params?.base_version || state.evolution.baseVersion;
      state.evolution.evolutionMode = data.params?.evolution_mode || state.evolution.evolutionMode;
    }
    renderEvolutionProgress();
  });

  source.addEventListener("job_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.status = data.status || "running";
    state.evolution.baseVersion = data.base_version || state.evolution.baseVersion;
    state.evolution.evolutionMode = data.evolution_mode || state.evolution.evolutionMode;
    addEvolutionLog(`Job started from ${state.evolution.baseVersion}: ${data.rounds + 1} versions x ${data.games_per_round} games.`);
    renderEvolutionProgress();
  });

  source.addEventListener("version_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.currentVersion = data.version_id;
    if (data.base_version) state.evolution.baseVersion = data.base_version;
    addEvolutionLog(`Version ${data.version_id} started${data.parent_id ? ` from ${data.parent_id}` : ""}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("llm_preflight_started", (message) => {
    const data = JSON.parse(message.data);
    addEvolutionLog(`LLM preflight started: ${data.llm_provider || "-"} ${data.llm_model || ""}.`);
  });

  source.addEventListener("llm_preflight_completed", (message) => {
    const data = JSON.parse(message.data);
    addEvolutionLog(`LLM preflight ok: ${data.llm_provider || "-"} ${data.llm_model || ""} / ${fixed(data.latency_ms)} ms.`);
  });

  source.addEventListener("llm_preflight_failed", (message) => {
    const data = JSON.parse(message.data);
    addEvolutionLog(`LLM preflight failed: ${data.error || "-"} ${data.message || ""}`);
  });

  source.addEventListener("game_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.currentVersion = data.batch?.version_id || data.version_id || state.evolution.currentVersion;
    state.evolution.currentGameIndex = Number(data.batch?.game_index || 0);
    addEvolutionLog(`${state.evolution.currentVersion} game ${state.evolution.currentGameIndex + 1} started.`);
    applyGameStart(data);
    renderEvolutionProgress();
  });

  source.addEventListener("game_event", (message) => {
    applyGameEvent(JSON.parse(message.data));
  });

  source.addEventListener("review_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    addEvolutionLog(`${data.version_id || state.evolution.currentVersion} game ${(data.batch?.game_index || 0) + 1} LLM review started.`);
    renderEvolutionProgress();
  });

  source.addEventListener("review_completed", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    const status = data.llm_review?.review_status || "unknown";
    addEvolutionLog(`${data.version_id || state.evolution.currentVersion} game ${(data.batch?.game_index || 0) + 1} LLM review ${status}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("game_completed", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.completedGames += 1;
      state.evolution.currentVersion = data.batch?.version_id || state.evolution.currentVersion;
      state.evolution.currentGameIndex = Number(data.batch?.game_index || state.evolution.currentGameIndex);
      addEvolutionLog(
        `${state.evolution.currentVersion} game ${state.evolution.currentGameIndex + 1} completed: ${data.game?.winner_zh || "-"} / score ${fixed(data.report?.scores?.overall)}.`,
      );
    }
    applyGameFinal(data);
    renderEvolutionProgress();
  });

  source.addEventListener("game_failed", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.failedGames += 1;
    state.evolution.currentVersion = data.version_id || state.evolution.currentVersion;
    state.evolution.currentGameIndex = Number(data.game_index || state.evolution.currentGameIndex);
    addEvolutionLog(`${state.evolution.currentVersion} game ${state.evolution.currentGameIndex + 1} failed: ${data.error}`);
    renderEvolutionProgress();
  });

  source.addEventListener("batch_aborted", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    addEvolutionLog(`${data.version_id || state.evolution.currentVersion} batch aborted: ${data.reason || "fatal error"}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("version_completed", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    const scores = data.aggregate?.average_scores || {};
    state.evolution.versions.push({
      version_id: data.version_id,
      promoted: Boolean(data.promoted),
      overall: scores.overall,
      vote_quality: scores.vote_quality,
      skill_quality: scores.skill_quality,
      mistakes_per_game: data.aggregate?.mistakes_per_game,
      memory_file: data.memory_file,
    });
    const promotionReason = data.promotion?.reason ? ` / ${data.promotion.reason}` : "";
    addEvolutionLog(
      `Version ${data.version_id} completed: overall ${fixed(scores.overall)}, mistakes ${fixed(data.aggregate?.mistakes_per_game)}, promoted ${Boolean(data.promoted)}${promotionReason}.`,
    );
    if (data.memory_file) addEvolutionLog(`Memory saved: ${shortPath(data.memory_file)}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("job_completed", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.status = "completed";
      state.evolution.finalResult = data.result;
      addEvolutionLog(`Job completed. Final version: ${data.result?.final_version || "-"}.`);
      if (data.result?.latest_promoted_file) addEvolutionLog(`Latest promoted pointer: ${shortPath(data.result.latest_promoted_file)}.`);
    }
    source.close();
    if (state.evolutionSource === source) state.evolutionSource = null;
    renderEvolution(data.result);
  });

  source.addEventListener("job_failed", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.status = "failed";
      addEvolutionLog(`Job failed: ${data.error} ${data.message || ""}`);
    }
    source.close();
    if (state.evolutionSource === source) state.evolutionSource = null;
    renderEvolutionProgress();
  });

  source.onerror = () => {
    if (state.evolution?.status === "completed" || state.evolution?.status === "failed") {
      source.close();
      if (state.evolutionSource === source) state.evolutionSource = null;
      return;
    }
    addEvolutionLog("Evolution event stream disconnected; backend job may still be running.");
    renderEvolutionProgress();
  };
}

function subscribeFrozenEval(jobId) {
  const url = new URL("/api/frozen-eval/events", window.location.origin);
  url.searchParams.set("job_id", jobId);
  const source = new EventSource(url.toString());
  state.evolutionSource = source;

  source.addEventListener("job_snapshot", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.status = data.status || state.evolution.status;
    }
    renderEvolutionProgress();
  });

  source.addEventListener("job_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.status = data.status || "running";
    state.evolution.totalGames = Number(data.total_games || state.evolution.totalGames);
    addEvolutionLog(`Frozen eval started: ${data.version || "-"} vs ${data.baseline_version || "-"}, ${data.games || 0} games each.`);
    renderEvolutionProgress();
  });

  source.addEventListener("frozen_eval_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.currentVersion = data.version_id || state.evolution.candidateVersion || state.evolution.currentVersion;
    state.evolution.baseVersion = data.baseline_version_id || state.evolution.baseVersion;
    addEvolutionLog(`Writes disabled: memory bank, strategy snapshots, latest promoted.`);
    renderEvolutionProgress();
  });

  source.addEventListener("llm_preflight_started", (message) => {
    const data = JSON.parse(message.data);
    addEvolutionLog(`LLM preflight started: ${data.llm_provider || "-"} ${data.llm_model || ""}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("llm_preflight_completed", (message) => {
    const data = JSON.parse(message.data);
    addEvolutionLog(`LLM preflight ok: ${fixed(data.latency_ms)} ms.`);
    renderEvolutionProgress();
  });

  source.addEventListener("llm_preflight_failed", (message) => {
    const data = JSON.parse(message.data);
    addEvolutionLog(`LLM preflight failed: ${data.error || "-"} ${data.message || ""}`);
    renderEvolutionProgress();
  });

  source.addEventListener("game_started", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    const side = data.eval_side || "candidate";
    state.evolution.currentSide = side;
    state.evolution.currentVersion = data.batch?.version_id || data.version_id || state.evolution.currentVersion;
    state.evolution.currentGameIndex = Number(data.batch?.game_index || 0);
    addEvolutionLog(`${frozenSideLabel(side)} ${state.evolution.currentVersion} game ${state.evolution.currentGameIndex + 1} started.`);
    applyGameStart(data);
    renderEvolutionProgress();
  });

  source.addEventListener("game_event", (message) => {
    applyGameEvent(JSON.parse(message.data));
  });

  source.addEventListener("game_completed", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      const side = data.eval_side || state.evolution.currentSide || "candidate";
      state.evolution.completedGames += 1;
      state.evolution.currentSide = side;
      state.evolution.currentVersion = data.batch?.version_id || state.evolution.currentVersion;
      state.evolution.currentGameIndex = Number(data.batch?.game_index || state.evolution.currentGameIndex);
      addEvolutionLog(
        `${frozenSideLabel(side)} ${state.evolution.currentVersion} game ${state.evolution.currentGameIndex + 1} completed: ${data.game?.winner_zh || "-"} / score ${fixed(data.report?.scores?.overall)}.`,
      );
    }
    applyGameFinal(data);
    renderEvolutionProgress();
  });

  source.addEventListener("game_failed", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    const side = data.eval_side || state.evolution.currentSide || "candidate";
    state.evolution.failedGames += 1;
    state.evolution.currentSide = side;
    state.evolution.currentVersion = data.version_id || state.evolution.currentVersion;
    state.evolution.currentGameIndex = Number(data.game_index || state.evolution.currentGameIndex);
    addEvolutionLog(`${frozenSideLabel(side)} ${state.evolution.currentVersion} game ${state.evolution.currentGameIndex + 1} failed: ${data.error}`);
    renderEvolutionProgress();
  });

  source.addEventListener("batch_aborted", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    const side = data.eval_side || state.evolution.currentSide || "candidate";
    addEvolutionLog(`${frozenSideLabel(side)} ${data.version_id || state.evolution.currentVersion} batch aborted: ${data.reason || "fatal error"}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("frozen_eval_batch_completed", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    const scores = data.aggregate?.average_scores || {};
    state.evolution.versions.push({
      version_id: `${frozenSideLabel(data.eval_side)}: ${data.version_id}`,
      promoted: false,
      overall: scores.overall,
      vote_quality: scores.vote_quality,
      skill_quality: scores.skill_quality,
      mistakes_per_game: data.aggregate?.mistakes_per_game,
    });
    addEvolutionLog(`${frozenSideLabel(data.eval_side)} batch completed: overall ${fixed(scores.overall)}, mistakes ${fixed(data.aggregate?.mistakes_per_game)}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("frozen_eval_completed", (message) => {
    const data = JSON.parse(message.data);
    if (!state.evolution) return;
    state.evolution.finalResult = data;
    addEvolutionLog(`Frozen eval completed: overall delta ${fixed(data.delta?.overall)}, mistake delta ${fixed(data.delta?.mistakes_per_game)}.`);
    renderEvolutionProgress();
  });

  source.addEventListener("job_completed", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.status = "completed";
      state.evolution.finalResult = data.result;
      addEvolutionLog(`Frozen eval job completed.`);
    }
    source.close();
    if (state.evolutionSource === source) state.evolutionSource = null;
    renderFrozenEval(data.result);
  });

  source.addEventListener("job_failed", (message) => {
    const data = JSON.parse(message.data);
    if (state.evolution) {
      state.evolution.status = "failed";
      addEvolutionLog(`Frozen eval failed: ${data.error} ${data.message || ""}`);
    }
    source.close();
    if (state.evolutionSource === source) state.evolutionSource = null;
    renderEvolutionProgress();
  });

  source.onerror = () => {
    if (state.evolution?.status === "completed" || state.evolution?.status === "failed") {
      source.close();
      if (state.evolutionSource === source) state.evolutionSource = null;
      return;
    }
    addEvolutionLog("Frozen eval event stream disconnected; backend job may still be running.");
    renderEvolutionProgress();
  };
}

function frozenSideLabel(side) {
  return side === "baseline" ? "Baseline" : "Candidate";
}

async function fetchJson(url, options = undefined) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text);
  }
  return response.json();
}

async function loadVersionOptions() {
  try {
    const data = await fetchJson("/api/versions");
    const versions = Array.isArray(data.versions) ? data.versions : [];
    if (!versions.length) return;
    populateVersionSelect("versionSelect", versions, "latest");
    populateVersionSelect("baseMemorySelect", versions, "latest");
  } catch (error) {
    console.warn("Failed to load version options", error);
  }
}

function populateVersionSelect(id, versions, fallbackValue) {
  const select = document.getElementById(id);
  if (!select) return;
  const current = select.value || fallbackValue;
  select.innerHTML = versions
    .map((item) => {
      const value = item.version_id || "";
      const label = item.label || value;
      return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  const values = new Set([...select.options].map((option) => option.value));
  select.value = values.has(current) ? current : fallbackValue;
}

function buildRunUrl(path, params = {}) {
  const url = new URL(path, window.location.origin);
  const [llmProvider, llmModel] = document.getElementById("modelSelect").value.split("|");
  url.searchParams.set("llm_provider", llmProvider);
  if (llmModel) url.searchParams.set("llm_model", llmModel);
  Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
  return url.toString();
}

function boundedInput(id, min, max, fallback) {
  const value = Number(document.getElementById(id).value || fallback);
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, Math.round(value)));
}

function renderHumanPanel() {
  const panel = document.getElementById("humanPanel");
  const pill = document.getElementById("humanPill");
  const container = document.getElementById("humanPrompt");
  if (!panel || !pill || !container) return;
  const humanMode = state.human.mode === "human";
  panel.classList.toggle("active", humanMode);
  if (!humanMode) {
    pill.textContent = "Observer mode";
    container.innerHTML = `<div class="list-item">切换到 Human Player 后，你会以所选座位进入对局；直播过程中只显示你的私有视角，终局后再复盘完整日志。</div>`;
    return;
  }
  if (state.human.finalRevealed) {
    pill.textContent = "Game finished";
    container.innerHTML = `<div class="list-item">对局已结束，完整上帝视角日志与复盘已经开放。</div>`;
    return;
  }
  const prompt = state.human.pendingPrompt;
  if (!prompt) {
    const seat = state.human.playerId ? `P${state.human.playerId}` : "-";
    pill.textContent = `Playing ${seat}`;
    container.innerHTML = `<div class="list-item"><strong>当前进度</strong><br>${escapeHtml(
      state.liveStatus || "等待轮到你行动。AI 玩家会继续实时推进；投票阶段会等待你的同步投票。",
    )}</div>`;
    return;
  }
  pill.textContent = `D${prompt.day} ${prompt.phase_zh || prompt.phase}`;
  const legalOptions = (prompt.legal_actions || [])
    .map((action) => `<option value="${escapeHtml(action)}">${escapeHtml(actionLabel(action))}</option>`)
    .join("");
  container.innerHTML = `<div class="human-grid">
    <div class="human-brief">
      <div class="metric"><span>你的身份</span><strong>${escapeHtml(prompt.player?.role_zh || prompt.player?.role || "-")}</strong></div>
      <div class="metric"><span>座位</span><strong>${escapeHtml(prompt.player?.name || `P${prompt.human_player_id}`)}</strong></div>
      <div class="metric"><span>可选动作</span><strong>${escapeHtml((prompt.legal_actions || []).map(actionLabel).join(" / "))}</strong></div>
    </div>
    ${humanTeammateBlock(prompt)}
    <form id="humanDecisionForm" class="human-form">
      <label>
        <span>动作</span>
        <select id="humanActionSelect">${legalOptions}</select>
      </label>
      <label>
        <span>目标</span>
        <select id="humanTargetSelect"></select>
      </label>
      <label class="wide">
        <span>公开发言</span>
        <textarea id="humanSpeechInput" rows="4" placeholder="发言阶段填写；其他阶段可留空"></textarea>
      </label>
      <label class="wide">
        <span>理由</span>
        <textarea id="humanReasonInput" rows="3" placeholder="给复盘看的决策理由"></textarea>
      </label>
      <label>
        <span>置信度</span>
        <input id="humanConfidenceInput" type="number" min="0" max="100" value="80" />
      </label>
      <button type="submit" class="primary">${state.human.submitting ? "Submitting..." : "Submit"}</button>
    </form>
    <details class="human-details" open>
      <summary>你的私有信息</summary>
      <pre>${escapeHtml(JSON.stringify(humanPrivatePreview(prompt), null, 2))}</pre>
    </details>
    <details class="human-details">
      <summary>公开历史摘要</summary>
      <pre>${escapeHtml(JSON.stringify(prompt.public_history_tail || [], null, 2))}</pre>
    </details>
  </div>`;
  document.getElementById("humanDecisionForm").addEventListener("submit", submitHumanDecision);
  syncHumanForm();
  document.getElementById("humanActionSelect").addEventListener("change", syncHumanForm);
}

function syncHumanForm() {
  const action = document.getElementById("humanActionSelect")?.value;
  const targetSelect = document.getElementById("humanTargetSelect");
  const speechInput = document.getElementById("humanSpeechInput");
  if (!action || !targetSelect || !speechInput) return;
  const prompt = state.human.pendingPrompt || {};
  const targets = (prompt.target_options || []).filter((target) => (target.allowed_actions || []).includes(action));
  const needsTarget = actionRequiresTarget(action);
  targetSelect.innerHTML = [`<option value="">无目标</option>`]
    .concat(
      targets.map((target) => {
        const allowed = (target.allowed_actions || []).map(actionLabel).join("/");
        return `<option value="${target.id}">${escapeHtml(target.name || `P${target.id}`)}${allowed ? ` (${escapeHtml(allowed)})` : ""}</option>`;
      }),
    )
    .join("");
  targetSelect.disabled = !needsTarget;
  if (needsTarget && targets.length) {
    targetSelect.value = String(targets[0].id);
  } else {
    targetSelect.value = "";
  }
  speechInput.disabled = action !== "speak";
}

async function submitHumanDecision(event) {
  event.preventDefault();
  const prompt = state.human.pendingPrompt;
  if (!prompt || state.human.submitting) return;
  const body = {
    session_id: prompt.session_id,
    prompt_id: prompt.prompt_id,
    action: document.getElementById("humanActionSelect").value,
    target_id: document.getElementById("humanTargetSelect").value || null,
    speech: document.getElementById("humanSpeechInput").value,
    reason: document.getElementById("humanReasonInput").value,
    confidence: Number(document.getElementById("humanConfidenceInput").value || 80),
  };
  state.human.submitting = true;
  renderHumanPanel();
  try {
    await fetchJson("/api/human/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.human.pendingPrompt = null;
  } catch (error) {
    state.human.submitting = false;
    document.getElementById("humanPrompt").insertAdjacentHTML(
      "afterbegin",
      `<div class="list-item error">${escapeHtml(error.message || String(error))}</div>`,
    );
    return;
  }
  state.human.submitting = false;
  renderHumanPanel();
}

function humanPrivatePreview(prompt) {
  return {
    known_teammates: prompt.known_teammates || [],
    private_knowledge: prompt.private_knowledge || {},
    belief_state: prompt.belief_state || {},
    strategy_memory_tail: prompt.strategy_memory_tail || [],
    action_context: prompt.action_context || {},
    rules: prompt.game_rules || {},
  };
}

function humanTeammateBlock(prompt) {
  const teammates = prompt.known_teammates || [];
  if (!teammates.length) return "";
  return `<div class="human-teammates">
    <strong>已知狼队友</strong>
    ${teammates.map((player) => `<span>P${player.id} ${escapeHtml(player.name || "")}</span>`).join("")}
  </div>`;
}

function actionLabel(action) {
  return {
    kill: "刀人",
    inspect: "查验",
    save: "救人",
    poison: "毒人",
    speak: "发言",
    vote: "投票",
    shoot: "开枪",
    pass: "弃权/跳过",
  }[action] || action;
}

function actionRequiresTarget(action) {
  return ["kill", "inspect", "save", "poison", "vote", "shoot"].includes(action);
}

function renderAll() {
  renderSummary();
  renderStage();
  renderTimeline();
  renderReport();
  renderIoPlayerSelect();
  renderIoDebug();
  renderHumanPanel();
  syncEventControls();
}

function renderSummary() {
  const game = state.game;
  if (!game) return;
  const report = state.report;
  const runtime = state.human.mode === "human" && !state.human.finalRevealed ? "human+llm/private view" : game.events[0]?.private_payload?.agent_backend || "llm";
  const latency = latencyStats(game.events);
  const skillStats = skillUsageStats(game.events, state.skillUsageSummary);
  const items = [
    ["Status", state.streaming ? "live" : game.winner_zh],
    ["Days", `Day ${game.day}`],
    ["Alive", `${game.summary.alive_count} / 9`],
    ["Score", report ? fixed(report.scores.overall) : "pending"],
    ["LLM Avg", latency.count ? formatLatency(latency.avg) : "pending"],
    ["LLM Max", latency.count ? formatLatency(latency.max) : "pending"],
    ["Skill Use", state.human.mode === "human" && !state.human.finalRevealed ? "hidden" : skillStats.available ? `${skillStats.matchedDecisions}/${skillStats.availableDecisions}` : "none"],
    ["Runtime", runtime],
  ];
  document.getElementById("summaryBand").innerHTML = items
    .map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
  document.getElementById("winnerPill").textContent = state.streaming ? "Live game" : `${game.winner_zh} win`;
  document.getElementById("versionPill").textContent = `${game.version_id} / ${runtime}`;
}

function renderStage() {
  if (!state.game) return;
  const event = currentEvent();
  const phase = event ? event.phase : "setup";
  const isNight = phase.includes("night");
  const isDay = phase.includes("day") || phase === "dawn";
  const stage = document.getElementById("gameStage");
  stage.classList.toggle("night", isNight);
  stage.classList.toggle("day", isDay);

  const phasePill = document.getElementById("phasePill");
  phasePill.textContent = event ? `${event.phase_zh} · D${event.day}` : "Waiting";
  phasePill.classList.toggle("night", isNight);
  phasePill.classList.toggle("day", isDay);
  document.getElementById("coreIcon").textContent = isNight ? "☾" : "☼";
  document.getElementById("corePhase").textContent = event ? event.phase_zh : "Ready";
  document.getElementById("coreEvent").textContent = event ? event.public_text : "Run a game to start replay.";
  document.getElementById("coreResult").textContent = event ? eventResultText(event) : "";

  renderSeats(event);
  syncEventControls();
}

function renderSeats(event) {
  const playerMap = new Map(state.game.players.map((player) => [player.id, player]));
  const activeId = event?.actor_id || null;
  const targetIds = new Set(eventTargets(event));
  const voteMap = latestVotesUntil(state.currentIndex);
  const count = state.game.players.length;
  const html = state.game.players
    .map((player, index) => {
      const position = seatPosition(index, count);
      const badge = seatBadge(player, event, voteMap);
      const classes = [
        "seat",
        roleClass[player.role] || "unknown",
        player.alive ? "" : "dead",
        player.is_human ? "human-seat" : "",
        activeId === player.id ? "active" : "",
        targetIds.has(player.id) ? "target" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const status = player.alive ? "Alive" : `Dead D${player.death_day} ${reasonText(player.death_reason)}`;
      return `<article class="${classes}" style="--x:${position.x}%;--y:${position.y}%">
        <div class="avatar-row">
          <div class="avatar">${player.name.replace("P", "")}</div>
          <div>
            <div class="seat-name">${player.name}</div>
            <div class="seat-role">${escapeHtml(player.role_zh || "未知")} / ${escapeHtml(player.role || "unknown")}</div>
            <div class="seat-status">${status}</div>
          </div>
        </div>
        ${badge}
      </article>`;
    })
    .join("");
  document.getElementById("seatLayer").innerHTML = html;
}

function seatPosition(index, count) {
  const angle = -90 + (360 / count) * index;
  const rad = (angle * Math.PI) / 180;
  const x = 50 + Math.cos(rad) * 39;
  const y = 52 + Math.sin(rad) * 39;
  return { x: clamp(x, 9, 91), y: clamp(y, 10, 90) };
}

function seatBadge(player, event, voteMap) {
  if (event?.actor_id === player.id) {
    return `<span class="seat-badge night">acting: ${escapeHtml(event.decision?.action || event.event_type)}</span>`;
  }
  if (eventTargets(event).includes(player.id)) {
    return `<span class="seat-badge night">targeted</span>`;
  }
  const voted = voteMap.get(player.id);
  if (voted) {
    return `<span class="seat-badge vote">${voted === "abstain" ? "abstained" : `voted P${voted}`}</span>`;
  }
  return `<span class="seat-badge">${player.alive ? "watching" : "out"}</span>`;
}

function latestVotesUntil(index) {
  const votes = new Map();
  for (const event of state.game.events.slice(0, index + 1)) {
    if (event.event_type === "vote" && event.actor_id) {
      votes.set(event.actor_id, event.public_payload?.abstain ? "abstain" : event.target_id);
    }
    if (event.event_type === "dawn_deaths" || event.event_type === "dawn_peace") {
      votes.clear();
    }
  }
  return votes;
}

function eventTargets(event) {
  if (!event) return [];
  const ids = [];
  if (event.target_id) ids.push(event.target_id);
  if (event.secondary_target_id) ids.push(event.secondary_target_id);
  if (Array.isArray(event.public_payload?.death_ids)) ids.push(...event.public_payload.death_ids);
  if (event.public_payload?.exiled_id) ids.push(event.public_payload.exiled_id);
  if (event.public_payload?.shot_target) ids.push(event.public_payload.shot_target);
  if (!(state.human.mode === "human" && state.streaming)) {
    if (event.private_payload?.target_id) ids.push(event.private_payload.target_id);
    if (event.private_payload?.attack_target) ids.push(event.private_payload.attack_target);
    if (event.private_payload?.saved_target) ids.push(event.private_payload.saved_target);
    if (event.private_payload?.poisoned_target) ids.push(event.private_payload.poisoned_target);
  }
  return [...new Set(ids.filter(Boolean))];
}

function renderTimeline() {
  if (!state.game) return;
  const events = state.game.events.filter((event) => {
    const hidden = event.public_payload && event.public_payload.visible_to_players === false;
    if (state.filter === "public") return !hidden;
    if (state.filter === "hidden") return hidden;
    return true;
  });
  document.getElementById("timeline").innerHTML = events
    .map((event) => {
      const hidden = event.public_payload && event.public_payload.visible_to_players === false;
      const reason = event.private_payload && event.private_payload.reason ? event.private_payload.reason : "";
      const decision = event.decision ? decisionText(event.decision) : "";
      return `<article class="event ${hidden ? "hidden" : ""} ${event.id === currentEvent()?.id ? "current" : ""}" data-event-id="${event.id}">
        <div class="event-phase">D${event.day}<br>${event.phase_zh}</div>
        <div>
          <div class="event-text">${escapeHtml(event.public_text)}</div>
          ${decision ? `<div class="event-reason">${escapeHtml(decision)}</div>` : ""}
          ${reason ? `<div class="event-reason">Reason: ${escapeHtml(reason)}</div>` : ""}
        </div>
      </article>`;
    })
    .join("");
  document.querySelectorAll(".event[data-event-id]").forEach((item) => {
    item.addEventListener("click", () => {
      pausePlayback();
      const id = Number(item.dataset.eventId);
      const index = state.game.events.findIndex((event) => event.id === id);
      if (index >= 0) setCurrentIndex(index);
    });
  });
}

function renderReport() {
  document.getElementById("archivePill").textContent = state.archivePath
    ? `Log ${state.archivePath.split(/[\\/]/).pop()}`
    : "Log pending";
  document.getElementById("archivePill").title = state.archivePath || "";
  if (!state.report) {
    document.getElementById("scoreBars").innerHTML = `<div class="list-item">Review will appear after the streamed game finishes.</div>`;
    document.getElementById("mistakeList").innerHTML = `<div class="list-item">Pending final report.</div>`;
    document.getElementById("memoryList").innerHTML = `<div class="list-item">Strategy memory is available during play in Agent I/O.</div>`;
    return;
  }
  const scores = state.report.scores;
  document.getElementById("scoreBars").innerHTML = Object.entries(scoreLabels)
    .map(([key, label]) => {
      const value = Number(scores[key] || 0);
      return `<div class="score-row">
        <span>${label}</span>
        <div class="bar"><div class="bar-fill" style="width:${Math.max(0, Math.min(100, value))}%"></div></div>
        <strong>${fixed(value)}</strong>
      </div>`;
    })
    .join("");

  const mistakes = state.report.mistakes.length
    ? state.report.mistakes
        .slice(0, 6)
        .map((item) => `<div class="list-item"><strong>${escapeHtml(item.severity)}</strong> D${item.day} ${escapeHtml(item.message)}</div>`)
        .join("")
    : `<div class="list-item">No high-risk mistake detected.</div>`;
  document.getElementById("mistakeList").innerHTML = mistakes;

  const reviewRows = llmReviewRows(state.llmReview);
  const role = document.getElementById("versionSelect").value === "evolved" ? "evolved" : "initial";
  const memories = Object.values(state.strategies || {})
    .flatMap((profile) => (profile.strategy_memory || []).slice(-2).map((memory) => `${profile.role}: ${memory}`))
    .slice(0, 8);
  const memoryRows = reviewRows.length ? reviewRows.concat(memories.map((item) => `${role} ${item}`)).slice(0, 10) : memories.map((item) => `${role} ${item}`);
  document.getElementById("memoryList").innerHTML = memoryRows
    .map((item) => `<div class="list-item">${escapeHtml(item)}</div>`)
    .join("");
}

function llmReviewRows(review) {
  if (!review) return [];
  if (review.review_status && review.review_status !== "ok") {
    return [`LLM复盘 ${review.review_status}: ${review.message || review.error || "-"}`];
  }
  const rows = [];
  if (review.game_summary) rows.push(`LLM复盘：${review.game_summary}`);
  (review.good_cases || []).slice(0, 2).forEach((item) => {
    rows.push(`做得好 ${item.role || "-"} P${item.player_id || "-"}：${item.lesson || item.reason || "-"}`);
  });
  (review.bad_cases || []).slice(0, 2).forEach((item) => {
    rows.push(`待改进 ${item.role || "-"} P${item.player_id || "-"}：${item.lesson || item.reason || "-"}`);
  });
  (review.memory_candidates || []).slice(0, 4).forEach((item) => {
    rows.push(`记忆候选 ${item.role || "-"}：${item.memory || "-"}`);
  });
  return rows;
}

function renderIoPlayerSelect() {
  const select = document.getElementById("ioPlayerSelect");
  const current = select.value || "all";
  const options = [`<option value="all">All</option>`]
    .concat(
      state.game.players.map(
        (player) => `<option value="${player.id}">${player.name} / ${player.role_zh}</option>`,
      ),
    )
    .join("");
  select.innerHTML = options;
  select.value = [...select.options].some((option) => option.value === current) ? current : "all";
}

function renderIoDebug() {
  if (!state.game) return;
  if (state.human.mode === "human" && !state.human.finalRevealed) {
    document.getElementById("ioDebug").innerHTML = `<div class="list-item">Human mode hides Agent I/O during live play. Full logs are available after the game ends.</div>`;
    return;
  }
  const selected = document.getElementById("ioPlayerSelect").value || "all";
  const playerMap = new Map(state.game.players.map((player) => [player.id, player]));
  const events = state.game.events.filter((event) => {
    if (!event.decision) return false;
    if (selected === "all") return true;
    return String(event.actor_id) === selected;
  });

  document.getElementById("ioDebug").innerHTML = events.length
    ? events.map((event) => renderIoCard(event, playerMap)).join("")
    : `<div class="list-item">No agent decision for this filter.</div>`;
}

function renderIoCard(event, playerMap) {
  const actor = playerMap.get(event.actor_id) || {};
  const decision = event.decision || {};
  const observation = event.observation_snapshot || {};
  const backend = decision.metadata ? decision.metadata.decision_backend || "llm" : "llm";
  const model = decision.metadata ? [decision.metadata.llm_provider, decision.metadata.llm_model].filter(Boolean).join("/") : "";
  const evidenceIds = decision.metadata?.evidence_event_ids || decision.metadata?.raw_decision?.evidence_event_ids || [];
  const quotedEvidence = decision.metadata?.quoted_evidence || decision.metadata?.raw_decision?.quoted_evidence || [];
  const deceptionIntent = decision.metadata?.deception_intent || decision.metadata?.raw_decision?.deception_intent || "";
  const coverStory = decision.metadata?.public_cover_story || decision.metadata?.raw_decision?.public_cover_story || "";
  const latencyMs = decisionLatencyMs(decision);
  const callCount = decision.metadata?.llm_call_count;
  const skillUsage = decision.metadata?.skill_usage || {};
  const skillMatches = skillUsage.matched_skills || [];
  const promptSkillRecall = decision.metadata?.prompt_skill_recall || [];
  const result = eventResultText(event);
  const inputPreview = {
    day: observation.day,
    phase: observation.phase,
    self_id: observation.self_id,
    self_role: observation.self_role,
    legal_actions: observation.legal_actions,
    alive_players: observation.alive_players,
    private_knowledge: observation.private_knowledge,
    strategy_memory: observation.strategy_memory,
    retrieved_memories: observation.retrieved_memories,
    belief_state: observation.belief_state,
    player_profile: observation.player_profile,
    game_rules: observation.game_rules,
    public_history_tail: (observation.public_history || []).slice(-8),
  };
  return `<article class="io-card">
    <div class="io-title">
      <span>${escapeHtml(actor.name || `P${event.actor_id}`)} / ${escapeHtml(actor.role_zh || decision.role || "")}</span>
      <span>D${event.day} ${escapeHtml(event.phase_zh)} / ${escapeHtml(model || backend)}</span>
    </div>
    <div class="io-output">
      <div class="io-chip"><span>Action</span>${escapeHtml(decision.action || "-")}</div>
      <div class="io-chip"><span>Target</span>${decision.target_id ? `P${decision.target_id}` : "-"}</div>
      <div class="io-chip"><span>Confidence</span>${decision.confidence !== undefined ? fixed(decision.confidence * 100) : "-"}</div>
      <div class="io-chip"><span>Latency</span>${latencyMs === null ? "-" : formatLatency(latencyMs)}</div>
      <div class="io-chip"><span>Calls</span>${callCount || "-"}</div>
      <div class="io-chip"><span>Prompt Skills</span>${decision.metadata?.skill_recall_selected_count !== undefined ? `${decision.metadata.skill_recall_selected_count}/${decision.metadata.skill_recall_available_count || 0}` : "-"}</div>
      <div class="io-chip"><span>Skills</span>${skillUsage.available_count ? `${skillUsage.matched_count || 0}/${skillUsage.available_count}` : "-"}</div>
      <div class="io-chip"><span>Event</span>${escapeHtml(event.event_type)}</div>
    </div>
    ${decision.speech ? `<div class="io-text">Speech: ${escapeHtml(decision.speech)}</div>` : ""}
    ${decision.reason ? `<div class="io-text">Reason: ${escapeHtml(decision.reason)}</div>` : ""}
    ${deceptionIntent ? `<div class="io-text">Deception: ${escapeHtml(deceptionIntent)}${coverStory ? ` / ${escapeHtml(coverStory)}` : ""}</div>` : ""}
    ${
      evidenceIds.length || quotedEvidence.length
        ? `<div class="io-text">Evidence: ${evidenceIds.map((id) => `#${id}`).join(", ") || "-"}${quotedEvidence.length ? ` · ${escapeHtml(quotedEvidence.join(" / "))}` : ""}</div>`
        : ""
    }
    ${result ? `<div class="io-result">Result: ${escapeHtml(result)}</div>` : ""}
    ${
      skillMatches.length
        ? `<div class="io-text">Skill usage: ${escapeHtml(
            skillMatches.map((skill) => `${skill.title || skill.skill_id} (${fixed(skill.score * 100)}%)`).join(" / "),
          )}</div>`
        : ""
    }
    ${
      promptSkillRecall.length
        ? `<div class="io-text">Prompt skill recall: ${escapeHtml(
            promptSkillRecall
              .map((skill) => `${skill.title || skill.skill_id} (${fixed((skill.recall_score || 0) * 100)}%)`)
              .join(" / "),
          )}</div>`
        : ""
    }
    <details class="io-input">
      <summary>Input PrivateObservation</summary>
      <pre class="io-json">${escapeHtml(JSON.stringify(inputPreview, null, 2))}</pre>
    </details>
  </article>`;
}

function renderAb(data) {
  document.getElementById("abPill").textContent = `Delta ${fixed(data.delta.overall)}`;
  const items = [
    ["Model", [data.llm_provider, data.llm_model].filter(Boolean).join("/") || "default"],
    ["Overall delta", fixed(data.delta.overall)],
    ["Mistake delta", fixed(data.delta.mistakes_per_game)],
    ["Good evolved win", percent(data.matchups.evolved_good_vs_initial_wolves.evolved_side_win_rate)],
    ["Wolf evolved win", percent(data.matchups.evolved_wolves_vs_initial_good.evolved_side_win_rate)],
  ];
  document.getElementById("abResult").innerHTML = items
    .map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");
  renderLeaderboard([
    {
      version_id: "initial",
      promoted: true,
      overall: data.initial.average_scores.overall,
      vote_quality: data.initial.average_scores.vote_quality,
      skill_quality: data.initial.average_scores.skill_quality,
      mistakes_per_game: data.initial.mistakes_per_game,
    },
    {
      version_id: "evolved",
      promoted: true,
      overall: data.evolved.average_scores.overall,
      vote_quality: data.evolved.average_scores.vote_quality,
      skill_quality: data.evolved.average_scores.skill_quality,
      mistakes_per_game: data.evolved.mistakes_per_game,
    },
  ]);
}

function renderFrozenEval(data) {
  const candidateScores = data.candidate?.average_scores || {};
  const baselineScores = data.baseline?.average_scores || {};
  document.getElementById("abPill").textContent = `Frozen Delta ${fixed(data.delta?.overall)}`;
  const items = [
    ["Mode", "Frozen Eval"],
    ["Model", [data.llm_provider, data.llm_model].filter(Boolean).join("/") || "default"],
    ["Candidate", data.version_id || data.requested_version || "-"],
    ["Baseline", data.baseline_version_id || data.requested_baseline_version || "-"],
    ["Games", `${data.completed_games?.candidate || 0} / ${data.completed_games?.baseline || 0}`],
    ["Overall delta", fixed(data.delta?.overall)],
    ["Mistake delta", fixed(data.delta?.mistakes_per_game)],
    ["Candidate mistakes", fixed(data.candidate?.mistakes_per_game)],
    ["Baseline mistakes", fixed(data.baseline?.mistakes_per_game)],
    ["Writes", data.frozen ? "disabled" : "enabled"],
  ];
  const notes = [
    data.interpretation || "",
    `Candidate archives: ${(data.archives?.candidate || []).length}`,
    `Baseline archives: ${(data.archives?.baseline || []).length}`,
    `Candidate errors: ${(data.errors?.candidate || []).length}`,
    `Baseline errors: ${(data.errors?.baseline || []).length}`,
  ];
  document.getElementById("abResult").innerHTML =
    items.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("") +
    `<div class="evolution-notes">${notes
      .filter(Boolean)
      .map((item) => `<div class="list-item">${escapeHtml(item)}</div>`)
      .join("")}</div>`;
  renderLeaderboard([
    {
      version_id: data.baseline_version_id || "baseline",
      promoted: false,
      overall: baselineScores.overall,
      vote_quality: baselineScores.vote_quality,
      skill_quality: baselineScores.skill_quality,
      mistakes_per_game: data.baseline?.mistakes_per_game,
    },
    {
      version_id: data.version_id || "candidate",
      promoted: false,
      overall: candidateScores.overall,
      vote_quality: candidateScores.vote_quality,
      skill_quality: candidateScores.skill_quality,
      mistakes_per_game: data.candidate?.mistakes_per_game,
    },
  ]);
}

function closeEvolutionStream() {
  if (state.evolutionSource) {
    state.evolutionSource.close();
    state.evolutionSource = null;
  }
}

function addEvolutionLog(text) {
  if (!state.evolution) return;
  state.evolution.logs.unshift(text);
  state.evolution.logs = state.evolution.logs.slice(0, 10);
}

function renderEvolutionProgress() {
  const evo = state.evolution;
  if (!evo) return;
  const isFrozen = evo.evolutionMode === "frozen_eval";
  const finished = evo.completedGames + evo.failedGames;
  const progressLabel = `${finished} / ${evo.totalGames}`;
  document.getElementById("abPill").textContent =
    evo.status === "completed" ? "Completed" : evo.status === "failed" ? "Failed" : `${isFrozen ? "Frozen" : "Evolving"} ${progressLabel}`;
  const items = [
    ["Status", evo.status || "running"],
    ["Mode", evo.evolutionMode || "workflow"],
    ["Job", evo.jobId || "-"],
    [isFrozen ? "Baseline" : "Base", evo.baseVersion || evo.requestedBaseVersion || "-"],
    ...(isFrozen ? [["Side", frozenSideLabel(evo.currentSide)], ["Candidate", evo.candidateVersion || "-"]] : []),
    ["Version", evo.currentVersion || "-"],
    ["Game", `${Number(evo.currentGameIndex || 0) + 1} / ${evo.gamesPerRound}`],
    ["Completed", String(evo.completedGames || 0)],
    ["Failed", String(evo.failedGames || 0)],
    ["Total", String(evo.totalGames || 0)],
    ["Progress", progressLabel],
  ];
  const logs = evo.logs.length
    ? `<div class="evolution-notes">${evo.logs.map((item) => `<div class="list-item">${escapeHtml(item)}</div>`).join("")}</div>`
    : "";
  document.getElementById("abResult").innerHTML =
    items.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("") +
    logs;
  if (evo.versions.length) renderLeaderboard(evo.versions);
}

function renderEvolution(data) {
  if (!data) {
    renderEvolutionProgress();
    return;
  }
  const finalVersion = data.final_version || "-";
  const promotedCount = (data.history || []).filter((item) => item.promoted).length;
  document.getElementById("abPill").textContent = `Final ${finalVersion}`;
  const latest = (data.history || [])[Math.max(0, (data.history || []).length - 1)] || {};
  const aggregate = latest.aggregate || {};
  const scores = aggregate.average_scores || {};
  const items = [
    ["Model", [data.llm_provider, data.llm_model].filter(Boolean).join("/") || "default"],
    ["Evolution mode", data.evolution_mode || "workflow"],
    ["Base", data.base_version || data.requested_base_version || "-"],
    ["Final version", finalVersion],
    ["Rounds", (data.history || []).length ? String((data.history || []).length - 1) : "0"],
    ["Promoted", String(promotedCount)],
    ["Overall", fixed(scores.overall)],
    ["Mistakes / game", fixed(aggregate.mistakes_per_game)],
    ["Villager wins", String(aggregate.winner_counts?.villagers || 0)],
    ["Werewolf wins", String(aggregate.winner_counts?.werewolves || 0)],
    ["Memory files", String((data.memory_files || []).length)],
    ["Run memory", data.memory_run_file ? shortPath(data.memory_run_file) : "-"],
    ["Latest pointer", data.latest_promoted_file ? shortPath(data.latest_promoted_file) : "-"],
    ["Memory bank", data.memory_bank_file ? shortPath(data.memory_bank_file) : "-"],
  ];
  document.getElementById("abResult").innerHTML =
    items.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("") +
    renderEvolutionReflections(data.history || []) +
    renderSkillCards(data.final_profiles || {}) +
    renderMemoryFiles(data);
  renderLeaderboard(data.leaderboard || []);
}

function renderEvolutionReflections(history) {
  const latest = history.length ? history[history.length - 1] : null;
  const reflections = latest?.reflections || {};
  const rows = Object.entries(reflections)
    .flatMap(([role, items]) => (items || []).slice(0, 2).map((item) => [role, item]))
    .slice(0, 8);
  if (!rows.length) return "";
  return `<div class="evolution-notes">
    ${rows.map(([role, item]) => `<div class="list-item"><strong>${escapeHtml(role)}</strong><br />${escapeHtml(item)}</div>`).join("")}
  </div>`;
}

function renderSkillCards(profiles) {
  const rows = Object.values(profiles || {})
    .flatMap((profile) => {
      const role = profile.role || "-";
      const skills = profile.parameters?.role_skills || [];
      return skills.slice(0, 2).map((skill) => [role, skill.title || skill.skill_id || "skill", skill.trigger || ""]);
    })
    .slice(0, 10);
  if (!rows.length) return "";
  return `<div class="evolution-notes">
    ${rows
      .map(([role, title, trigger]) => `<div class="list-item"><strong>${escapeHtml(role)} · ${escapeHtml(title)}</strong><br />${escapeHtml(trigger)}</div>`)
      .join("")}
  </div>`;
}

function renderMemoryFiles(data) {
  const files = data.memory_files || [];
  if (!files.length && !data.memory_run_file) return "";
  const rows = [];
  if (data.memory_run_file) rows.push(["run", data.memory_run_file]);
  if (data.latest_promoted_file) rows.push(["latest_promoted", data.latest_promoted_file]);
  if (data.memory_bank_file) rows.push(["memory_bank", data.memory_bank_file]);
  if (data.player_profiles_file) rows.push(["player_profiles", data.player_profiles_file]);
  files.forEach((item) => rows.push([item.version_id || "version", item.latest_path || item.snapshot_path]));
  return `<div class="evolution-notes">
    ${rows
      .map(([label, path]) => `<div class="list-item"><strong>${escapeHtml(label)}</strong><br />${escapeHtml(path)}</div>`)
      .join("")}
  </div>`;
}

function renderLeaderboard(rows) {
  if (!rows.length) {
    document.getElementById("leaderboard").innerHTML = `<div class="list-item">No leaderboard data.</div>`;
    return;
  }
  const header = `<div class="leader-row header"><span>Version</span><span>Overall</span><span>Mistakes</span><span>Vote</span><span>Skill</span></div>`;
  const body = rows
    .map(
      (row) => `<div class="leader-row">
        <span>${escapeHtml(row.version_id)}${row.promoted ? " / promoted" : ""}</span>
        <span>${fixed(row.overall)}</span>
        <span>${fixed(row.mistakes_per_game)}</span>
        <span>${fixed(row.vote_quality)}</span>
        <span>${fixed(row.skill_quality)}</span>
      </div>`,
    )
    .join("");
  document.getElementById("leaderboard").innerHTML = header + body;
}

function togglePlayback() {
  if (state.playing) {
    pausePlayback();
  } else {
    startPlayback();
  }
}

function startPlayback() {
  if (!state.game) return;
  state.playing = true;
  document.getElementById("playPauseBtn").textContent = "Pause";
  const speed = Number(document.getElementById("speedSelect").value || 2200);
  state.timer = setInterval(() => {
    if (state.currentIndex >= state.game.events.length - 1) {
      pausePlayback();
      return;
    }
    setCurrentIndex(state.currentIndex + 1);
  }, speed);
}

function pausePlayback() {
  state.playing = false;
  document.getElementById("playPauseBtn").textContent = "Play";
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
}

function stepEvent(delta) {
  pausePlayback();
  setCurrentIndex(state.currentIndex + delta);
}

function setCurrentIndex(index) {
  if (!state.game) return;
  state.currentIndex = clamp(index, 0, Math.max(0, state.game.events.length - 1));
  renderStage();
  renderTimeline();
}

function syncEventControls() {
  if (!state.game) return;
  const slider = document.getElementById("eventSlider");
  slider.max = Math.max(0, state.game.events.length - 1);
  slider.value = state.currentIndex;
  document.getElementById("eventCounter").textContent = state.game.events.length
    ? `${state.currentIndex + 1} / ${state.game.events.length}`
    : "0 / 0";
}

function currentEvent() {
  return state.game?.events?.[state.currentIndex] || null;
}

function eventResultText(event) {
  if (!event) return "";
  if (event.event_type === "wolf_pack_plan") {
    const options = (event.private_payload?.strategy_options || [])
      .map((item) => item.id || item.name_zh)
      .filter(Boolean)
      .slice(0, 4)
      .join(", ");
    return `wolf strategy space${options ? `: ${options}` : ""}`;
  }
  if (event.event_type === "wolf_council_plan") {
    const target = event.private_payload?.attack_target || event.target_id || "-";
    const status = event.private_payload?.coordination_status || "planned";
    return `wolf council ${status}: attack P${target}`;
  }
  if (event.event_type === "wolf_intent") return `wolf target P${event.private_payload.target_id || "-"}`;
  if (event.event_type === "seer_inspect") return `inspect P${event.private_payload.target_id || "-"} = ${event.private_payload.result || "-"}`;
  if (event.event_type === "witch_action") {
    const saved = event.private_payload.saved_target ? `save P${event.private_payload.saved_target}` : "";
    const poisoned = event.private_payload.poisoned_target ? `poison P${event.private_payload.poisoned_target}` : "";
    return [saved, poisoned].filter(Boolean).join(", ") || "no potion";
  }
  if (event.event_type === "vote") {
    const prefix = event.public_payload?.revote ? "PK revote" : "vote";
    return event.public_payload?.abstain ? `${prefix} abstain` : `${prefix} P${event.target_id || "-"}`;
  }
  if (event.event_type === "vote_tie") {
    const targets = (event.public_payload?.tied_targets || []).map((id) => `P${id}`).join(", ");
    return `tie PK: ${targets}`;
  }
  if (event.event_type === "hunter_shot") return `shoot P${event.target_id || "-"}`;
  if (event.event_type === "speech") return event.decision?.speech || "public speech recorded";
  if (event.event_type === "exile") {
    const exiled = event.public_payload?.exiled_id || event.target_id;
    return exiled ? `exile P${exiled}` : "no exile";
  }
  if (event.event_type === "dawn_deaths") return `dead: ${(event.public_payload?.death_ids || []).map((id) => `P${id}`).join(", ")}`;
  if (event.event_type === "dawn_peace") return "peaceful night";
  return event.public_text || "";
}

function decisionText(decision) {
  const target = decision.target_id ? ` -> P${decision.target_id}` : "";
  const confidence = decision.confidence !== undefined ? ` confidence ${fixed(decision.confidence * 100)}` : "";
  return `${decision.action}${target}${confidence}`;
}

function latencyStats(events) {
  const values = (events || [])
    .map((event) => decisionLatencyMs(event.decision))
    .filter((value) => value !== null);
  if (!values.length) return { count: 0, avg: 0, max: 0 };
  const total = values.reduce((sum, value) => sum + value, 0);
  return {
    count: values.length,
    avg: total / values.length,
    max: Math.max(...values),
  };
}

function skillUsageStats(events, summary) {
  if (summary?.total) {
    return {
      available: Number(summary.total.decisions_with_available_skills || 0) > 0,
      availableDecisions: Number(summary.total.decisions_with_available_skills || 0),
      matchedDecisions: Number(summary.total.decisions_with_matched_skills || 0),
    };
  }
  let availableDecisions = 0;
  let matchedDecisions = 0;
  for (const event of events || []) {
    const usage = event.decision?.metadata?.skill_usage;
    if (!usage?.available_count) continue;
    availableDecisions += 1;
    if (Number(usage.matched_count || 0) > 0) matchedDecisions += 1;
  }
  return {
    available: availableDecisions > 0,
    availableDecisions,
    matchedDecisions,
  };
}

function decisionLatencyMs(decision) {
  const metadata = decision?.metadata || {};
  const value = metadata.llm_latency_ms ?? metadata.llm_total_elapsed_ms;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatLatency(ms) {
  const value = Number(ms || 0);
  if (value >= 1000) return `${fixed(value / 1000)} s`;
  return `${fixed(value)} ms`;
}

function reasonText(reason) {
  const map = {
    werewolf_kill: "wolf kill",
    poison: "poison",
    exile: "exile",
    hunter_shot: "hunter shot",
  };
  return map[reason] || reason || "";
}

function fixed(value) {
  return Number(value || 0).toFixed(1);
}

function percent(value) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function shortPath(path) {
  const parts = String(path || "").split(/[\\/]/);
  return parts.slice(-2).join("/");
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function setLoading(id, text) {
  document.getElementById(id).innerHTML = `<div class="loading">${escapeHtml(text)}</div>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

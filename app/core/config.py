"""Application configuration.

Single source of truth for every environment-dependent value. Business logic
must read configuration from here and must not call ``os.getenv`` directly.

Audit references: M-6 (CWD-dependent config, import-time crash), C-1 (hardcoded
secret), H-3 (no timeouts/limits), P1-13 (robust configuration).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or unusable.

    Exists so a misconfigured deployment fails with a readable message rather
    than a raw pydantic traceback at import time (FR-1.6).
    """


class Settings(BaseSettings):
    """Every environment-dependent value the application uses.

    Secrets use ``SecretStr`` so they cannot be emitted by an accidental
    ``str()``, ``repr()``, or f-string in a log record (SR-5).
    """

    model_config = SettingsConfigDict(
        # Absolute path: settings must resolve identically regardless of the
        # directory the process was launched from (FR-1.7, M-6).
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Telegram (C-1, P0-1, P0-2) -----------------------------------------
    BOT_TOKEN: SecretStr

    # --- AI provider: OpenAI-compatible endpoint ---------------------------
    GROQ_API_KEY: SecretStr
    # Defaults are the values Phase 2 validated against the live endpoint. The
    # endpoint default is overridden in `.env`; the model default matches a model
    # the provider actually serves, so a fresh install is not pointed at a
    # retired one.
    LLM_BASE_URL: str = "https://api.groq.com/openai/v1"
    LLM_MODEL: str = "qwen/qwen3.8-27b"
    # Which provider implementation to build. The value selects a branch in
    # app.infrastructure.llm.create_llm_client and is the documented extension
    # point that makes the choice reversible (FR-16, T-6). Adding a provider
    # means adding a branch there, not changing the service layer.
    LLM_PROVIDER: str = "openai_compatible"

    # --- AI provider reliability (H-3, P0-11, P0-12) -----------------------
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    LLM_MAX_TOKENS: int = Field(default=1024, gt=0)
    LLM_TEMPERATURE: float = Field(default=0.3, ge=0.0, le=2.0)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=10)

    # --- Model fallback chain (FR-32, FR-33, FR-34) ------------------------
    # Comma-separated, tried in order, only for retryable failures. Bounded by
    # LLM_FALLBACK_MAX_ATTEMPTS so a persistent failure cannot multiply cost.
    LLM_FALLBACK_MODELS: str = ""
    LLM_FALLBACK_MAX_ATTEMPTS: int = Field(default=1, ge=0, le=5)

    # --- Conversation context policy (FR-8, FR-9, FR-10) -------------------
    # A configuration decision, not a hardcoded constant (FR-9). The strategy is
    # a recent-window truncation: the newest LLM_CONTEXT_MAX_MESSAGES are kept,
    # then the oldest are dropped until the estimated total fits
    # LLM_CONTEXT_MAX_TOKENS. No summarization and no embeddings.
    LLM_CONTEXT_MAX_MESSAGES: int = Field(default=20, ge=2)
    LLM_CONTEXT_MAX_TOKENS: int = Field(default=4000, gt=0)
    # Divisor for the deterministic token estimate. See app.services.context.
    LLM_CHARS_PER_TOKEN: float = Field(default=4.0, gt=0)
    # Hard cap on one inbound user message, applied before persistence so an
    # oversized message cannot reach the provider or the database.
    CHAT_MAX_MESSAGE_CHARS: int = Field(default=4000, gt=0)

    # --- Telegram polling ---------------------------------------------------
    # Whether the ASGI process also runs the long poller.
    #
    # True by default, so a single-process deployment behaves exactly as it always
    # has: `uvicorn app.main:app` serves HTTP and polls. The compose stack sets it
    # false on `api` and true on the dedicated `bot` service, because two processes
    # calling getUpdates on one bot token is not a redundancy — Telegram delivers
    # each update to whichever consumer asks last, so messages are dropped at
    # random and the failure looks like a flaky bot rather than a duplicated one.
    #
    # Keep the default True. A deployment that only wants the HTTP surface is a
    # deliberate choice, not the absence of one.
    TELEGRAM_POLLING: bool = True
    # Where the standalone poller writes its heartbeat, and how often.
    # Only used by `python -m app.bot.poller`; the ASGI poller has no worker
    # process to prove anything to.
    BOT_HEARTBEAT_PATH: str = "/tmp/agentique-bot-heartbeat"
    BOT_HEARTBEAT_INTERVAL_SECONDS: float = Field(default=15.0, gt=0)

    # --- Celery (H-3, M-9, P0-13, P1-9) ------------------------------------
    REDIS_URL: str
    # Hard ceiling on one task. 120 s was measured to be **below** the pipeline's
    # real duration: a fully-successful 5-source run took 426 s claim-to-terminal
    # with DoH resolution, and a harder question was SIGKILLed at 120 s
    # mid-evidence, which is the failure that exposed the gap. The evidence stage
    # alone makes 1 + CLAIM_MAX_WINDOWS model calls per source, so the run is
    # tens of provider calls, not one.
    #
    # 900 s ≈ 2.1× the measured worst observed run, which leaves room for the
    # retry backoff the provider rate limit actually produces (a run that stalls
    # on 429s and retries adds minutes, not seconds). The worst-case *budget* is
    # far larger than the typical case — 27 provider calls at a 60 s timeout
    # each — so this remains a backstop, not a target. Tune it from a measured
    # run of your own, not from this default.
    #
    # Raising it is what makes the soft limit below meaningful: with a hard limit
    # under the pipeline's duration, the hard limit fires first, the worker is
    # SIGKILLed, no graceful handling runs at all, and the run is left `running`
    # for the next redelivery to trip over.
    CELERY_TASK_TIME_LIMIT: int = Field(default=900, gt=0)
    # Soft ceiling, strictly below the hard one. Crossing it raises
    # SoftTimeLimitExceeded *inside* the awaiting frame, which is the only signal
    # the task can act on: the run is marked failed, one terminal notice is sent,
    # and no retry re-spends the budget. The 120 s margin is the shutdown
    # allowance — enough to write one status transition and send one message.
    #
    # Must stay below CELERY_TASK_TIME_LIMIT or it does nothing; a validator
    # below enforces it at construction rather than at the moment it is needed.
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=780, gt=0)
    CELERY_TASK_MAX_RETRIES: int = Field(default=2, ge=0, le=10)
    CELERY_TASK_RETRY_BACKOFF: int = Field(default=5, ge=0)
    CELERY_WORKER_PREFETCH_MULTIPLIER: int = Field(default=1, ge=1)

    # --- Cost control (M-8, P1-8) -----------------------------------------
    RATE_LIMIT_MAX_REQUESTS: int = Field(default=5, gt=0)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, gt=0)
    MAX_IN_FLIGHT_REQUESTS: int = Field(default=8, gt=0)
    # Daily allowance per user (FR-28). Enforced by the same in-process limiter
    # as the sliding window, not by a second limiter (AD-012).
    RATE_LIMIT_DAILY_MAX_REQUESTS: int = Field(default=200, gt=0)
    RATE_LIMIT_DAILY_WINDOW_SECONDS: int = Field(default=86400, gt=0)

    # --- Telegram output (C-4, P0-8, P0-9) --------------------------------
    TELEGRAM_PARSE_MODE: str = "HTML"
    TELEGRAM_MAX_MESSAGE_LENGTH: int = Field(default=4096, gt=0)

    # --- Observability (H-5, P1-3) ----------------------------------------
    HEALTH_PROBE_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0)
    LOG_LEVEL: str = "INFO"

    # --- Persistence (Phase 2 § 6) -----------------------------------------
    # Required as of Phase 2: the chat flow cannot run without it, and leaving
    # it optional would let the process start and then fail on first use.
    DATABASE_URL: str
    DATABASE_ECHO: bool = False
    DATABASE_POOL_SIZE: int = Field(default=5, gt=0)
    DATABASE_POOL_MAX_OVERFLOW: int = Field(default=5, ge=0)
    # Test-only. The database the suite is permitted to touch. Its name must end
    # in "_test"; app.db.session refuses anything else, so a misconfigured value
    # fails loudly instead of truncating real data (Phase 2 § 23).
    TEST_DATABASE_URL: str | None = None

    # --- Web search (Phase 3, P3.2, AD-036) -------------------------------------
    # The Research Engine searches through the `SearchProvider` port. Which
    # concrete provider is built is configuration, selected by a branch in
    # app.infrastructure.search.create_search_provider — the same extension point
    # shape as LLM_PROVIDER (FR-16, T-3). Two adapters exist ("brave",
    # "tavily"); "tavily" is the default now that a usable credential for it
    # exists, and the Brave adapter remains registered behind the port so the
    # configuration can be switched back without a code change.
    SEARCH_PROVIDER: str = "tavily"
    # The provider credential. Optional at the settings level on purpose: nothing
    # searches yet (the research pipeline is P3.9), so a deployment that does not
    # research must still start. The factory refuses to build a provider without
    # it, so the failure lands where search is actually used rather than at
    # startup — and it is a configuration failure, never a silent no-op (FR-6).
    SEARCH_API_KEY: SecretStr | None = None
    # Result count per query. Bounded (SR-5): one query can never ask for an
    # unbounded response, and every stage downstream of this inherits the bound.
    SEARCH_MAX_RESULTS: int = Field(default=5, ge=1, le=20)
    # Client-side deadline for one provider call. Explicit rather than inherited
    # from the LLM timeout (P0-11): a search call is cheap, fast, and on the
    # critical path of a run whose own budget is finite (FR-44).
    SEARCH_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0)
    # Language and market hints for queries that do not carry their own. Empty
    # by default: a search request is built from the user's own question, and the
    # pipeline must not silently bias it toward one market.
    SEARCH_LANGUAGE: str = ""
    SEARCH_REGION: str = ""
    # Safe search. "strict" by default because the product serves arbitrary
    # user questions in a public chat (SR-2); a provider that cannot honour the
    # level is not a usable adapter.
    SEARCH_SAFE_SEARCH: str = "strict"

    # --- Web fetching (Phase 3, P3.3, AD-031) ---------------------------------
    # The SSRF boundary. Every one of these is a bound the fetch stage enforces
    # on untrusted URLs, and none of them can be turned off by configuration —
    # `create_fetch_client` refuses to build if they are unset or nonsensical.
    # The frozen contract (§ 15.5) requires them to be configurable, so they are
    # settings rather than constants; the *policy* (which addresses are reachable
    # at all) is not configurable, because a configuration value that can permit
    # `10.0.0.0/8` is not a security control.
    #
    # Allowed schemes. Narrowing is permitted; widening is not. A value naming
    # `file` or `gopher` raises at construction rather than being honoured, so a
    # typo cannot quietly widen what the process will fetch (FR-13).
    FETCH_ALLOWED_SCHEMES: str = "http,https"
    # Media types the pipeline processes, checked before any body is read
    # (FR-17). Extending this is a decision for the extraction stage; the
    # default is the frozen minimum.
    FETCH_ALLOWED_CONTENT_TYPES: str = "text/html,text/plain"
    # Three separate bounds, because a single timeout cannot express them: a
    # connect timeout stops a black-holed address, a read timeout stops a slow
    # body, and the total deadline stops a body that dribbles just inside the
    # read timeout forever (FR-18). The total is the one that actually bounds
    # the operation.
    FETCH_CONNECT_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0)
    FETCH_READ_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)
    FETCH_TOTAL_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    # Hard ceiling on the *decoded* body, enforced while streaming. Bounded
    # because a chunked response has no Content-Length and a compressed one
    # describes its compressed size (FR-16, SR-5).
    FETCH_MAX_RESPONSE_BYTES: int = Field(default=5_000_000, gt=0)
    # Second decompression defence: even a body under the byte cap may not
    # expand past this multiple of its wire size. A 5 MB ceiling alone would
    # still permit ~1 GB of allocation from a 5 KB gzip stream.
    FETCH_MAX_COMPRESSION_RATIO: int = Field(default=200, gt=0)
    # Redirect depth. Each hop costs a full validation round, so this bounds both
    # work and the number of SSRF attempts a single URL can provoke (FR-15).
    FETCH_MAX_REDIRECTS: int = Field(default=5, ge=0, le=20)
    # Identifies the client to origins. Not a secret and not a credential; it
    # exists so an operator can recognise research traffic in origin logs.
    FETCH_USER_AGENT: str = "telegram-research-bot/1.0 (+research-fetch)"
    # Connection pool sizing. Small on purpose: the fetcher is not a crawler and
    # an unbounded pool is a resource-exhaustion surface of its own.
    FETCH_MAX_CONNECTIONS: int = Field(default=10, gt=0)
    FETCH_MAX_KEEPALIVE_CONNECTIONS: int = Field(default=5, ge=0)
    FETCH_KEEPALIVE_EXPIRY_SECONDS: float = Field(default=5.0, ge=0)

    # --- Fetch DNS resolution (Phase 3, P3.3, AD-031) -------------------------
    # How the fetch boundary turns a hostname into addresses to validate.
    #
    # "system" is the platform getaddrinfo — the default, unchanged behaviour,
    # and correct wherever the platform resolver answers honestly.
    #
    # "doh" resolves through a DNS-over-HTTPS endpoint instead. It exists for one
    # measured situation: a host whose network intercepts outbound UDP/53 and
    # rewrites every answer to a non-routable fake IP (Tailscale MagicDNS and
    # similar fake-IP pools return 198.18.0.0/15 — RFC 2544 benchmarking — which
    # the SSRF boundary then correctly refuses). HTTPS is not rewritten, so the
    # answers come back real and globally routable, and the address policy stays
    # exactly as it is. This is strictly better than relaxing the policy: the
    # boundary is untouched and the deployment opts in.
    #
    # Not a fallback. A deployment either resolves honestly through the platform
    # or says so here; there is no "try both", because a resolver that silently
    # disagrees with itself is worse than one that fails.
    FETCH_DNS_MODE: str = "system"
    # The RFC 8484 endpoints used when FETCH_DNS_MODE=doh, in the order they are
    # tried. Comma-separated. Each is tried in turn only when the one before it
    # could not be *reached or understood* — a transport failure, a non-200, or a
    # malformed answer. An endpoint that answers is used as-is, even when it
    # answers "no records": a legitimate NXDOMAIN must not be masked by asking a
    # different provider, because which provider answered is then not something
    # the deployment can reason about.
    #
    # Any DoH provider works; the wire format is the standard one, so this is not
    # a vendor API. The default lists two independent operators on purpose — a
    # single endpoint is a single point of failure for the whole fetch stage —
    # and a deployment should point this at a resolver it trusts or runs.
    FETCH_DNS_SERVERS: str = (
        "https://cloudflare-dns.com/dns-query,https://dns.google/dns-query"
    )
    # Bound on one DNS-over-HTTPS round trip, per attempt. The fetch timeout is
    # not reused on purpose: a resolver that hangs must not consume a fetch's
    # whole budget.
    FETCH_DOH_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0)


    # The extractor interface. Only one implementation exists (trafilatura);
    # adding another requires a branch in the factory, not a domain change.
    EXTRACT_EXTRACTOR: str = "trafilatura"
    # Upper bound on extracted text length (SR-5). Beyond this content is
    # truncated with an explicit marker rather than dropped silently.
    EXTRACT_MAX_CHARS: int = Field(default=100_000, gt=0)
    # Minimum readable characters to consider a result non-empty (FR-23).
    EXTRACT_MIN_CHARS: int = Field(default=10, ge=0)

    # --- Deduplication and ranking (Phase 3, P3.5) ---------------------------
    # Identity rules for collapsing candidates. Both default on; either may be
    # turned off, and each turning one off is a deliberate loss of recall rather
    # than a performance knob:
    #   * `DEDUP_GROUP_BY_URL` collapses equal canonical URLs — the only rule that
    #     still works when extraction failed for a source.
    #   * `DEDUP_GROUP_BY_CONTENT` collapses equal extracted-content fingerprints
    #     — the only rule that catches syndicated copies at different URLs (FR-24),
    #     and safe from the FR-25 boilerplate problem because it compares P3.4's
    #     extracted main content, never the raw page.
    DEDUP_GROUP_BY_URL: bool = True
    DEDUP_GROUP_BY_CONTENT: bool = True
    # Minimum extraction quality a source must reach to be kept (FR-30). Below it
    # the source is *recorded as excluded with a reason*, not silently dropped, so
    # a later stage can still count and explain it (FR-42).
    RANKING_MIN_QUALITY: float = Field(default=0.10, ge=0.0, le=1.0)

    # --- Research planning (Phase 3, P3.6) ---------------------------------
    # Every planning limit is a cost or latency bound, which is why they are
    # configuration rather than constants: each one becomes at least one provider
    # call or one stage of waiting downstream, so the right value is an
    # operational judgement an operator can change without a code edit.
    #
    # Steps per plan. `ResearchPlan` also enforces a hard structural ceiling
    # (MAX_STEPS_CEILING in app/domain/research/planning.py) that configuration
    # cannot raise, so a misconfigured value cannot permit an unbounded plan.
    PLAN_MAX_STEPS: int = Field(default=6, ge=1, le=25)
    # Bound on the research question itself. Keeps the prompt bounded (a large
    # unbounded prompt is both a cost and a context-window hazard) and rejects an
    # input that is not a question before any model call is made.
    PLAN_MAX_QUESTION_CHARS: int = Field(default=2_000, gt=0)
    # Output bound for the planning call. Larger than LLM_MAX_TOKENS because a plan
    # is structurally longer than a chat reply; separate so raising the chat bound
    # does not silently change what planning can produce.
    PLAN_MAX_TOKENS: int = Field(default=1_500, gt=0)

    # --- Per-source summarization and claim extraction (Phase 3, P3.7) -------
    # Same reasoning as the planning limits: each of these is a cost or latency
    # bound, and each becomes provider calls downstream. P3.7 makes
    # 1 + CLAIM_MAX_WINDOWS calls per source, so these multiply by the number of
    # selected sources — which is why they are explicit rather than inherited from
    # the chat bounds (a chat reply has nothing in common with a structured
    # evidence extraction).
    #
    # Input budget for the summary call, in estimated tokens. Whole passages only,
    # and `plan_chunk_windows` reports how many did not fit, so a summary of a
    # long source is marked truncated rather than silently covering part of it
    # (§ 16, FR-42).
    SUMMARY_MAX_INPUT_TOKENS: int = Field(default=4_000, gt=0)
    # Output bound for the summary call.
    SUMMARY_MAX_TOKENS: int = Field(default=800, gt=0)
    # Input budget for ONE claim-extraction call, in estimated tokens. A window is
    # built from whole passages to fit this, so a quote the model reads is a quote
    # that can be verified afterwards — evidence is never cut mid-passage.
    CLAIM_MAX_INPUT_TOKENS: int = Field(default=4_000, gt=0)
    # Output bound for the claim-extraction call.
    CLAIM_MAX_TOKENS: int = Field(default=1_200, gt=0)
    # Hard ceiling on windows (provider calls) per source. A source with more
    # passages than this budget can show has its remainder *counted* as uncovered
    # evidence, never silently skipped.
    CLAIM_MAX_WINDOWS: int = Field(default=4, ge=1, le=20)
    # Claims requested from one call. `MAX_CLAIMS_CEILING` in
    # app/domain/research/evidence.py is the structural ceiling this cannot
    # exceed.
    CLAIM_MAX_CLAIMS: int = Field(default=8, ge=1, le=20)

    # --- Research execution bounds (Phase 3, P3.9-D; AD-047, § B5-P2) ---------
    #
    # Three separate quantities were conflated before this stage, and § S.3.4
    # named the conflation as the reason no deadline value could be derived:
    #
    #   candidates — how many distinct URLs the plan's queries surface
    #   fetched    — how many of those the pipeline actually retrieves
    #   selected   — how many survive ranking and reach the evidence stage
    #
    # ``RESEARCH_MAX_SOURCES`` bounds the **middle one**, because that is where the
    # expensive work begins: a fetch costs up to ``FETCH_TOTAL_TIMEOUT_SECONDS`` of
    # wall clock, and every source selected for evidence costs
    # ``1 + CLAIM_MAX_WINDOWS`` model calls. Bounding the *candidates* alone would
    # still leave the fetch count a function of ``SEARCH_MAX_RESULTS × plan steps``;
    # bounding the *selected* alone would bound the cost but not the wall clock,
    # which is what a deadline has to reason about.
    #
    # The same bound is applied at ranking, so a run with more surviving sources
    # than the budget keeps its highest-ranked ones and records the rest as excluded
    # with a reason (FR-30) rather than dropping them silently (FR-42).
    #
    # **The default requires measurement before production confidence** (§ S.7).
    # 5 is the conservative choice for a documented operational reason rather than a
    # number picked to look deliberate: it equals ``SEARCH_MAX_RESULTS``, the
    # per-query bound P3.2 already justified, so a default run's work stays inside
    # "what one search step offers" no matter how many steps the plan contains. It
    # is **not** a claim that 5 sources answer a question well, that a run of 5
    # finishes inside any deadline, or that 5 is the right number — none of those has
    # been measured, because no run has ever executed (``SEARCH_API_KEY`` is absent
    # from this environment, § S.7).
    RESEARCH_MAX_SOURCES: int = Field(default=5, ge=1, le=20)

    # The cooperative run deadline (AD-045, FR-44). **Unset by default, and that
    # is the honest state of it.**
    #
    # No measurement exists: wall-clock duration per stage has never been recorded,
    # the number of sources a run fetches had no bound until this stage, and no
    # credentialed end-to-end run has executed (audit § S.7). A number chosen here
    # would look deliberate and be unmeasurable, which is precisely the failure § S.7
    # was written to prevent.
    #
    # So the setting exists and defaults to disabled. The *mechanism* is implemented
    # and tested (``app.domain.research.outcome.CooperativeDeadline``); the value is
    # left explicit as an operator decision. A deployment that never sets this has
    # no cooperative deadline, and runs are then bounded by the per-provider
    # timeouts (search 15 s, fetch 30 s total, LLM 60 s), the candidate bound above,
    # and Celery's own limits — a state the audit records rather than hides.
    #
    # Do not "fix" the None default by inventing a value; measure a run first.
    RESEARCH_DEADLINE_SECONDS: int | None = Field(default=None, gt=0)

    # --- Cross-source synthesis and citations (Phase 3, P3.8) ----------------
    # Same reasoning as the planning and P3.7 limits: each of these is a cost,
    # latency, or output bound, and each is an operational judgement an operator can
    # change without a code edit. P3.8 makes exactly ONE provider call per run, so
    # unlike P3.7 these do not multiply by the number of sources — which is what makes
    # cross-source synthesis affordable, and also why the evidence budget below is
    # the sharpest bound in this file: it is the only thing standing between a
    # twenty-source run and a prompt the model cannot read.
    #
    # Input budget for the synthesis call, in estimated tokens. What is offered is
    # "verified claim + verified quote + source metadata", never whole fetched pages,
    # and the highest-ranked evidence is offered first. What does not fit is
    # *counted* and reported on the result, never silently dropped, so an answer
    # synthesized from a truncated context can be distinguished from a complete one
    # (FR-42, SR-12).
    SYNTHESIS_MAX_INPUT_TOKENS: int = Field(default=8_000, gt=0)
    # Evidence items offered to one call. `MAX_EVIDENCE_CEILING` in
    # app/domain/research/synthesis.py is the structural ceiling this cannot exceed.
    SYNTHESIS_MAX_EVIDENCE: int = Field(default=30, ge=1, le=120)
    # Sections requested from one call. A section is the unit of citation
    # completeness (FR-36): each must name at least one citation that resolves, so
    # this bounds both the output and the number of units that must verify.
    # `MAX_SECTIONS_CEILING` is the structural ceiling.
    SYNTHESIS_MAX_SECTIONS: int = Field(default=6, ge=1, le=20)
    # Output bound for the synthesis call. Larger than LLM_MAX_TOKENS because a
    # structured multi-section answer is structurally longer than a chat reply, and
    # separate so raising the chat bound does not silently change what synthesis can
    # produce.
    SYNTHESIS_MAX_TOKENS: int = Field(default=2_000, gt=0)

    # --- Deployment mode ----------------------------------------------------
    ENVIRONMENT: str = "development"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def search_enabled(self) -> bool:
        """Whether search can actually run.

        True only when a provider is selected *and* a credential is present.
        Callers that need search fail loudly instead of silently degrading when
        this is False (SR-12).
        """
        return bool(self.SEARCH_PROVIDER.strip()) and self.SEARCH_API_KEY is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build settings once per process.

    A missing or malformed variable raises :class:`ConfigurationError` naming
    the offending field, instead of an import-time traceback (FR-1.6).
    """
    try:
        # Required values are supplied by the environment or the env file,
        # not by keyword arguments, so the constructor call takes none.
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError and friends
        fields = sorted(getattr(exc, "fields", None) or _field_names_from(exc))
        detail = ", ".join(fields) if fields else str(exc)
        raise ConfigurationError(
            f"Invalid configuration. Checked {ENV_FILE} and the environment. "
            f"Problem field(s): {detail}. "
            "See .env.example for the required variables."
        ) from None


def _field_names_from(exc: Exception) -> list[str]:
    """Best-effort extraction of offending field names from a pydantic error."""
    text = str(exc)
    names: list[str] = []
    for chunk in text.split("; "):
        head = chunk.strip()
        if head and head[0].isalpha() and head.replace("_", "").isalnum():
            names.append(head.split(" ")[0])
    return names

import os

from dotenv import load_dotenv

load_dotenv()

# No API keys - everything runs against a local Ollama server.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
AUDITOR_MODEL = os.environ.get("AUDITOR_MODEL", "qwen2.5-coder:7b")
FIXER_MODEL = os.environ.get("FIXER_MODEL", "qwen2.5-coder:7b")
# The model that approves or rejects each proposed fix. Defaults to the
# auditor's model; a different (e.g. stronger) one makes the check independent
# of the model that wrote the fix.
VERIFIER_MODEL = os.environ.get("VERIFIER_MODEL") or AUDITOR_MODEL
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "5"))

# Ollama defaults to a 2048-token context window, which silently truncates
# anything bigger - too small for a full file + prompt. 8192 is a safer
# floor on 16GB of unified memory; raise it if you have the RAM to spare.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

# Most file content the Fixer shows the model at once. Smaller files are
# shown whole; larger ones as a window around the finding's line (the Fixer
# makes targeted edits, so it never needs to re-emit the whole file). Raise
# it alongside OLLAMA_NUM_CTX if you're running a bigger model/context.
FIXER_MAX_FILE_CHARS = int(os.environ.get("FIXER_MAX_FILE_CHARS", "12000"))

# The Auditor's deep review reads real source files one at a time and asks
# the LLM to reason about each - that's one LLM call per file, so we cap
# both how many files and how much of each file, to keep a full run bounded
# on a local model. Raise AUDIT_MAX_FILES for a more thorough (slower) pass.
AUDIT_MAX_FILES = int(os.environ.get("AUDIT_MAX_FILES", "30"))
AUDIT_MAX_FILE_CHARS = int(os.environ.get("AUDIT_MAX_FILE_CHARS", "12000"))

# Where PDF reports are written, relative to wherever ZweiGeeks itself runs
# from (not the audited repo - keeps reports out of the repo you're auditing).
REPORTS_DIR = os.environ.get("REPORTS_DIR", "audit_results")

# Hard wall-clock cap per single Ollama call. If the local runner wedges -
# shows as "Stopping..." forever in `ollama ps` - a plain chat() call can
# hang indefinitely with no exception ever raised. This makes it fail fast.
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))

# If this many Ollama calls in a row time out, stop trying more files/fixes
# in this run instead of grinding through the rest at OLLAMA_TIMEOUT_SECONDS
# each - that's a wedged server, not a slow one, and no amount of waiting
# fixes it without restarting Ollama.
OLLAMA_MAX_CONSECUTIVE_TIMEOUTS = int(os.environ.get("OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", "3"))

# On a timeout, try once to recover before giving up: kill whatever is
# listening on the Ollama port (the Ollama app/launchd restarts it), wait a
# few seconds, and retry the call. Only applies when OLLAMA_HOST is local.
# Set to "0" to disable.
OLLAMA_AUTO_RESTART_ON_TIMEOUT = os.environ.get("OLLAMA_AUTO_RESTART_ON_TIMEOUT", "1") == "1"

# The Fixer asks for a full-file rewrite, which on a local 7B model can
# legitimately take well over OLLAMA_TIMEOUT_SECONDS for a mid-sized file -
# so it gets its own, longer cap. Slow is not the same as wedged.
FIXER_TIMEOUT_SECONDS = int(os.environ.get("FIXER_TIMEOUT_SECONDS", "300"))

# Fixed sampling seed for every Ollama call. Without one, the same file gets
# different findings on each pass (2 blocking one pass, 17 the next on a
# real PHP repo), so a finding can "disappear" by chance rather than by being
# fixed. A seed makes output repeatable for identical input.
# Don't reach for temperature 0 instead: greedy decoding sends
# qwen2.5-coder:7b into a repetition loop that runs until the timeout.
OLLAMA_SEED = int(os.environ.get("OLLAMA_SEED", "42"))
# Optional temperature override; unset keeps the model's own default.
OLLAMA_TEMPERATURE = (
    float(os.environ["OLLAMA_TEMPERATURE"]) if os.environ.get("OLLAMA_TEMPERATURE") else None
)

# Hard cap on tokens generated per Ollama call. Without one, qwen2.5-coder:7b
# occasionally falls into a repetition loop and generates indefinitely (seen:
# 19,000+ tokens from one fixer call). Ollama keeps serving that request even
# after our timeout gives up on it, every later call queues behind it, and
# `ollama ps` shows the model stuck on "Stopping..." - the wedge the timeout
# handling above exists for. Sized so a capped reply still finishes inside
# its timeout at ~10-20 tokens/s on a 16GB M-series Mac.
OLLAMA_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "1024"))
# Fixer edits can legitimately be longer (several SEARCH/REPLACE blocks).
FIXER_NUM_PREDICT = int(os.environ.get("FIXER_NUM_PREDICT", "2048"))

# After writing a Python fix, import the edited module with the project's
# own interpreter (its venv/.venv if there is one). If it imported before
# the edit and doesn't after, the edit is rolled back and rejected.
FIXER_IMPORT_CHECK = os.environ.get("FIXER_IMPORT_CHECK", "1") != "0"
FIXER_IMPORT_TIMEOUT_SECONDS = int(os.environ.get("FIXER_IMPORT_TIMEOUT_SECONDS", "120"))

# --- making the audit -> fix loop converge ---------------------------------

# Lowest severity the LLM Fixer works on. Findings are attempted most
# severe first; anything below this is reported but never sent to the model
# (ruff's own safe fixes and dependency pin bumps run regardless).
FIXER_MIN_SEVERITY = os.environ.get("FIXER_MIN_SEVERITY", "medium").lower()
# Most findings the LLM Fixer attempts in one pass (most severe first).
FIXER_MAX_FIXES_PER_PASS = int(os.environ.get("FIXER_MAX_FIXES_PER_PASS", "8"))
# A finding whose fixes keep being rejected is retried - with the rejection
# reason in the prompt and a different seed - at most this many times, then
# marked needs_manual_fix.
FIXER_MAX_ATTEMPTS_PER_FINDING = int(os.environ.get("FIXER_MAX_ATTEMPTS_PER_FINDING", "3"))

# Cache of per-file LLM reviews, keyed by file path, content hash, model and
# prompt version: re-audits only re-review files that changed. Set
# REVIEW_CACHE=0 to always ask the model. Defaults to <REPORTS_DIR>/.review_cache.
REVIEW_CACHE = os.environ.get("REVIEW_CACHE", "1") != "0"
REVIEW_CACHE_DIR = os.environ.get("REVIEW_CACHE_DIR")


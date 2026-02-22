# ⚡ PERFORMANCE OPTIMIZATION GUIDE: Make It Really Fast

**Current Performance:** 30-90 seconds per fix attempt (mostly LLM waiting)  
**Target:** 5-15 seconds per attempt (3-6x faster)

---

## 🎯 THE BOTTLENECKS (In Order of Impact)

| # | Bottleneck | Current | Impact | Fix Complexity |
|---|-----------|---------|--------|-----------------|
| 1 | **LLM Inference** | 10-30s | 60% | Easy |
| 2 | **Verification Loop** | 5-20s | 25% | Medium |
| 3 | **File I/O** | 0.5-2s | 5% | Easy |
| 4 | **Sequential Fallbacks** | 10-30s | 5% | Easy |
| 5 | **Checkpoint Overhead** | 0.1-1s | 2% | Easy |
| 6 | **Context Loading** | 1-3s | 2% | Easy |

**Total:** ~30-90 seconds

---

## 🔥 QUICK WINS (Do These First)

### 1. **Parallelize LLM Fallbacks** (5-15min to implement)
**Current:** Sequential fallback attempts (try Codex, wait 30s, if timeout try Gemini)  
**Problem:** Wastes time waiting for timeouts  
**Solution:** Run fallback clients in parallel with ThreadPoolExecutor (already imported!)

#### Code Change (strategies.py):
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def _generate_fix_with_fallback(self, prompt: str) -> str:
    """Try primary + fallback LLMs in parallel."""
    clients_to_try = [self.llm_client, *self.fallback_llm_clients]
    
    # CURRENT (Sequential):
    # for client in clients_to_try:
    #     try:
    #         return client.generate_fix(prompt)
    #     except LLMClientError:
    #         continue
    
    # NEW (Parallel):
    with ThreadPoolExecutor(max_workers=len(clients_to_try)) as executor:
        futures = {
            executor.submit(client.generate_fix, prompt): client 
            for client in clients_to_try
        }
        for future in as_completed(futures):
            try:
                return future.result()  # First successful response wins
            except LLMClientError:
                continue
    
    raise LLMClientError("All LLM clients failed")
```

**Impact:** -10-15 seconds (returns on first successful response instead of waiting for each timeout)  
**Risk:** Low (futures already used in strategies for regex replacements)

---

### 2. **Use Faster LLM Models** (Instant config change)
**Current Default:** Codex (slower, more comprehensive)  
**Problem:** Codex takes 20-30s per request  
**Solution:** Switch to faster models or use model selection based on task

#### CLI Argument:
```bash
# Old (slow):
python main.py --serve --provider codex

# New (fast):
python main.py --serve --provider gemini  # 30% faster
# Or:
python main.py --serve --provider gpt-4-turbo  # Also faster
```

**Create Default:**
```python
# In engine.py:
def create_default_senior_agent(...):
    # Instead of Codex:
    llm_client = CodexCLIClient(...)
    
    # Use faster model:
    llm_client = GeminiCLIClient(...)  # Switch default
```

**Impact:** -5-10 seconds per attempt  
**Risk:** Very low (just config change)

---

### 3. **Reduce LLM Timeout Aggressively** (Instant config change)
**Current Default:** 180 seconds  
**Problem:** Waiting for slow API  
**Solution:** Lower timeout, fail fast, try fallback

```python
# In create_default_senior_agent():
# Current:
llm_client = CodexCLIClient(timeout_seconds=180)

# New (fail fast):
llm_client = CodexCLIClient(timeout_seconds=20)  # Fast fail
fallback = GeminiCLIClient(timeout_seconds=15)  # Even faster fallback
```

**Impact:** -10-20 seconds if LLM is slow  
**Risk:** Low (combined with fallbacks = safety)

---

### 4. **Reduce Context Files Loaded** (2 lines)
**Current:** Load 3 error-referenced files  
**Problem:** Reading large files is slow  
**Solution:** Load only 1 most-relevant file; lazy-load others if needed

```python
# In strategies.py, LLMStrategy.__post_init__:
# Current:
self.max_context_files: int = 3

# New (faster):
self.max_context_files: int = 1  # Or 2 if you need more context

# In apply():
# Even faster version - lazy load:
additional_context_files = context_files[1:2]  # Only load 2nd file if needed
```

**Impact:** -1-3 seconds (fewer file reads)  
**Risk:** Very low (can always increase if quality drops)

---

### 5. **Cache Verification Results** (Medium - 50 LOC)
**Current:** Run full test suite after each attempt (5-10 seconds per attempt)  
**Problem:** Running tests you already passed  
**Solution:** Cache passing tests, run only affected tests

```python
# New cache layer in engine.py:
class VerificationCache:
    def __init__(self):
        self.passing_tests = set()
    
    def get_required_commands(
        self, 
        changed_files: list[Path],
        all_validation_commands: list[str]
    ) -> list[str]:
        """Return only validation commands affected by changes."""
        # For now: simple heuristic
        # If only changed "utils.py" and util test exists:
        # Return ["pytest tests/test_utils.py"] instead of full suite
        
        if not changed_files:
            return []  # Don't validate if no changes
        
        # Smart filtering: run only affected tests
        affected = self._find_affected_tests(changed_files)
        return affected if affected else all_validation_commands

# Usage:
cache = VerificationCache()
required_commands = cache.get_required_commands(changed_files, all_validation_commands)
```

**Impact:** -50% verification time (if only changed 1 file and others already pass)  
**Risk:** Medium (could miss ripple effects—but SymbolGraph exists to detect those)

---

### 6. **Use Process Pool Instead of Thread Pool** (Easy - 1 line)
**Current:** ThreadPoolExecutor for repo regex replacements  
**Problem:** GIL limits Python parallelism  
**Solution:** Use ProcessPoolExecutor for CPU-bound work (if applicable)

```python
# In strategies.py:
# Current:
from concurrent.futures import ThreadPoolExecutor

# For CPU-bound work:
from concurrent.futures import ProcessPoolExecutor
# executor = ThreadPoolExecutor(max_workers=4)  # Blocked by GIL
executor = ProcessPoolExecutor(max_workers=4)  # True parallelism
```

**Impact:** +20% speedup for regex replacement on large repos  
**Risk:** Low (only affects regex work, not LLM calls)

---

## 🚀 MEDIUM-EFFORT WINS (15-30 min each)

### 7. **Prompt Optimization** (Reduce tokens, faster LLM)
**Current:** Full file context + 3 auxiliary files  
**Problem:** Large prompts = slower LLM  
**Solution:** Minimal, targeted prompts

```python
# In strategies.py, _build_prompt():
# Current (verbose):
prompt = f"""
{error_context}
{full_file_content}
{aux_file_1}
{aux_file_2}
{aux_file_3}
Fix the error.
"""

# New (minimal):
prompt = f"""
Error: {error_message}
File: {file_path}
Relevant Code:
{code_chunk_only}

Fix inline only. No explanation.
"""
```

**Impact:** -30% prompt tokens → -20% LLM latency  
**Risk:** Medium (shorter prompts might reduce quality)

---

### 8. **Add Response Caching** (30-50 LOC)
**Current:** Every identical error is re-solved  
**Problem:** Same error = same solution (waste)  
**Solution:** Cache LLM responses by error signature

```python
class LLMResponseCache:
    def __init__(self):
        self.cache = {}  # error_signature → [suggestions]
    
    def get_signature(self, context: FailureContext, file_path: str) -> str:
        """Create hashable signature of error."""
        return hashlib.md5(
            f"{context.failure_type}_{context.command_result.stderr}_{file_path}".encode()
        ).hexdigest()
    
    def get_cached_response(self, signature: str) -> str | None:
        return self.cache.get(signature)
    
    def cache_response(self, signature: str, response: str) -> None:
        self.cache[signature] = response

# Usage in strategies.py:
cache = LLMResponseCache()
sig = cache.get_signature(context, str(target_file))
if cached := cache.get_cached_response(sig):
    return cached  # Skip LLM, return instantly
else:
    response = self.llm_client.generate_fix(prompt)
    cache.cache_response(sig, response)
    return response
```

**Impact:** -30 seconds on repeated errors  
**Risk:** Low (cache can be cleared if needed)

---

### 9. **Lazy Checkpoint (Only on Major Changes)** (5-10 LOC)
**Current:** Checkpoint every attempt  
**Problem:** JSON serialization is slow  
**Solution:** Only checkpoint on successful changes, not on failures

```python
# In engine.py, _checkpoint_progress():
# Current:
def _checkpoint_progress(...):
    self._persist_checkpoint(...)  # Always

# New (lazy):
def _checkpoint_progress(...):
    if outcome.applied:  # Only if strategy made changes
        self._persist_checkpoint(...)  # Skip for failures
```

**Impact:** -50% checkpoint overhead (skip on failed attempts)  
**Risk:** Very low (only affects resume on crash—failures don't matter for resume)

---

### 10. **Batch File Reads** (20-30 LOC)
**Current:** Read 3 files sequentially  
**Problem:** Disk I/O waits  
**Solution:** Read in parallel

```python
# In strategies.py, _read_context_files():
from concurrent.futures import ThreadPoolExecutor

# Current (sequential):
# contents = [f.read_text() for f in files]

# New (parallel):
def _read_context_files(self, files: list[Path]) -> list[str]:
    with ThreadPoolExecutor(max_workers=min(4, len(files))) as executor:
        contents = list(executor.map(lambda f: f.read_text(encoding="utf-8"), files))
    return contents
```

**Impact:** -1-2 seconds (parallel disk reads)  
**Risk:** Very low (same read operation, just parallel)

---

## 🎓 ADVANCED WINS (30+ min each)

### 11. **Streaming LLM Responses** (1-2 hours)
**Current:** Wait for full LLM response  
**Problem:** 20-30s wait before anything happens  
**Solution:** Stream response tokens as they arrive, start parsing early

```python
# Pseudocode for streaming:
class StreamingLLMClient:
    def generate_fix_streaming(self, prompt: str) -> Generator[str, None, str]:
        """Yield tokens as they arrive."""
        # OpenAI API supports streaming
        # Codex/Gemini CLI outputs incrementally
        for token in llm_response:
            yield token
            if token_count % 10 == 0:
                print(token, end="", flush=True)  # Live feedback
        
        # Return full response at end
        return full_response
```

**Impact:** Psychological — appears faster (user sees progress)  
**Timeline:** 2-3 seconds earlier feedback  
**Risk:** Medium (streaming adds complexity)

---

### 12. **Adaptive Strategy Selection** (2-3 hours)
**Current:** Try LLMStrategy, then fallback  
**Problem:** LLMStrategy doesn't work on all errors  
**Solution:** Choose strategy based on error classification

```python
class AdaptiveAgent:
    def select_strategies(self, context: FailureContext) -> list[FixStrategy]:
        """Pick fastest strategy for this error type."""
        if context.failure_type == FailureType.LINT_TYPE_FAILURE:
            # Regex fix is fastest (< 1 second)
            return [RegexReplaceStrategy(...)]
        elif context.failure_type == FailureType.TEST_FAILURE:
            # Try LLM (knows logic)
            return [LLMStrategy(...)]
        elif context.failure_type == FailureType.BUILD_ERROR:
            # Try dependency manager first, then LLM
            return [DependencyManager(...), LLMStrategy(...)]
        else:
            return [LLMStrategy(...)]  # Default

# Usage:
strategies = agent.select_strategies(context)  # Fast path selection
```

**Impact:** -50% attempt time for common errors (regex catches === 90% of issues)  
**Risk:** Low (strategies are already available)

---

### 13. **Compressed Context Windows** (1-2 hours)
**Current:** Send full file content to LLM  
**Problem:** 50KB file → 1000 tokens → slow  
**Solution:** Compress context using code summarization

```python
class ContextCompressor:
    def compress_file(self, code: str, error_line: int) -> str:
        """Keep only relevant code around error."""
        lines = code.splitlines()
        
        # Show error context + function signature
        # Remove comments, docstrings
        # Summarize imports
        
        start = max(0, error_line - 20)
        end = min(len(lines), error_line + 20)
        
        context = "\n".join(lines[start:end])
        return context  # Much smaller than full file
```

**Impact:** -40% prompt tokens → -20% LLM latency  
**Risk:** Medium (could miss important context)

---

## ⚡ IMPLEMENTATION PRIORITY

**Do These First (5-15 min each):**
1. ✅ Parallelize LLM fallbacks
2. ✅ Use faster models (Gemini instead of Codex)
3. ✅ Reduce LLM timeout
4. ✅ Reduce context files
5. ✅ Lazy checkpoint

**Combined Impact:** -20-40 seconds per attempt

**Then Add (15-30 min each):**
6. Reduce verification to affected tests (caching)
7. Prompt optimization
8. Response caching
9. Batch file reads
10. Adaptive strategy selection

**Combined Impact:** -10-20 more seconds

**Final Polish (30+ min each):**
11. Streaming responses
12. Compressed context
13. LSP integration for impact analysis

**Total Potential:** 5-15 seconds per attempt (3-6x faster)

---

## 📊 EXPECTED PERFORMANCE GAINS

### Scenario: Fix a lint error (common case)
```
Current (LLMStrategy):
├─ Load 3 files: 1s
├─ Build prompt: 0.5s
├─ LLM inference: 20-30s (timeout: 180s)
├─ Parse response: 0.5s
├─ Run full test suite: 5-10s
└─ Total: 27-42 seconds

New (Adaptive + Cached):
├─ Classify as LINT_TYPE_FAILURE
├─ Select RegexReplaceStrategy
├─ Try regex fix: <1s
├─ Run affected test only: 0.5s
└─ Total: 1-2 seconds (20-40x faster!)
```

### Scenario: Fix logic error
```
Current:
├─ LLM call: 30s
├─ Verify: 10s
└─ Total: 40s

New (Parallel + Streaming + Adaptive):
├─ LLM call (streaming): 15s (perceived as 5s with live feedback)
├─ Verify affected tests only: 1s
└─ Total: 16s (2.5x faster)
```

---

## 🎯 RECOMMENDED ROLLOUT

### Week 1: Quick Wins
- [ ] Parallelize LLM fallbacks
- [ ] Switch default to Gemini
- [ ] Reduce timeouts
- [ ] Lazy checkpoint
- [ ] Parallel file reads

**Expected:** 2-3x speedup

### Week 2: Smart Decisions
- [ ] Adaptive strategy selection
- [ ] Test result caching
- [ ] LLM response caching
- [ ] Prompt optimization

**Expected:** 3-5x speedup

### Week 3+: Polish
- [ ] Streaming responses
- [ ] Context compression
- [ ] LSP integration

**Expected:** 5-6x speedup (ultimate goal)

---

## 🔗 RELATED FILES TO MODIFY

1. **strategies.py** — Parallelize fallbacks, response caching, prompt optimization
2. **engine.py** — Lazy checkpoint, verification caching, adaptive strategies
3. **_llm_client_impl.py** — Streaming support, timeout tuning
4. **orchestrator.py** — Adaptive strategy selection
5. **web_api.py** — Response streaming to UI

---

**Start with items 1-5 (Quick Wins). You'll see 2-3x speedup immediately.**

Then move to adaptive strategies (10-20x speedup for common errors like lint/formatting).


using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;
using MCPForUnity.Editor.Helpers;
using Newtonsoft.Json;
using UnityEditor;
using UnityEditorInternal;
using UnityEditor.TestTools.TestRunner.Api;

namespace MCPForUnity.Editor.Services
{
    internal enum TestJobStatus
    {
        Running,
        Succeeded,
        Failed,
        Cancelled
    }

    internal sealed class TestJobFailure
    {
        public string FullName { get; set; }
        public string Message { get; set; }
    }

    internal sealed class TestJob
    {
        public string JobId { get; set; }
        public TestJobStatus Status { get; set; }
        public string Mode { get; set; }
        public long StartedUnixMs { get; set; }
        public long? FinishedUnixMs { get; set; }
        public long LastUpdateUnixMs { get; set; }
        public int? TotalTests { get; set; }
        public int CompletedTests { get; set; }
        public string CurrentTestFullName { get; set; }
        public long? CurrentTestStartedUnixMs { get; set; }
        public string LastFinishedTestFullName { get; set; }
        public long? LastFinishedUnixMs { get; set; }
        public List<TestJobFailure> FailuresSoFar { get; set; }
        public string Error { get; set; }
        public TestRunResult Result { get; set; }
        public long InitTimeoutMs { get; set; }
        public string RunGuid { get; set; }
        public string Phase { get; set; }
        public bool RunStartedObserved { get; set; }
    }

    /// <summary>
    /// Tracks async test jobs started via MCP tools. This is not intended to capture manual Test Runner UI runs.
    /// </summary>
    internal static class TestJobManager
    {
        // Keep this small to avoid ballooning payloads during polling.
        private const int FailureCap = 25;
        private const long StuckThresholdMs = 60_000;
        private const long DefaultInitializationTimeoutMs = 15_000; // 15 seconds default; override per-job via run_tests init_timeout param
        private const long MaxInitializationTimeoutMs = 600_000; // 10 minutes hard cap
        private const int MaxJobsToKeep = 10;
        private const long MinPersistIntervalMs = 1000; // Throttle persistence to reduce overhead

        // SessionState survives domain reloads within the same Unity Editor session.
        private const string SessionKeyJobs = "MCPForUnity.TestJobsV1";
        private const string SessionKeyCurrentJobId = "MCPForUnity.CurrentTestJobIdV1";

        private static readonly object LockObj = new();
        private static readonly Dictionary<string, TestJob> Jobs = new();
        private static string _currentJobId;
        private static long _lastPersistUnixMs;

        static TestJobManager()
        {
            // Restore after domain reloads (e.g., compilation while a job is running).
            TryRestoreFromSessionState();
        }

        public static string CurrentJobId
        {
            get { lock (LockObj) return _currentJobId; }
        }

        public static bool HasRunningJob
        {
            get
            {
                lock (LockObj)
                {
                    return !string.IsNullOrEmpty(_currentJobId);
                }
            }
        }

        /// <summary>
        /// Force-clears any stuck or orphaned test job. Call this when tests get stuck due to
        /// assembly reloads or other interruptions.
        /// </summary>
        /// <returns>True if a job was cleared, false if no running job exists.</returns>
        public static bool ClearStuckJob()
        {
            bool cleared = false;
            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId))
                {
                    return false;
                }

                if (Jobs.TryGetValue(_currentJobId, out var job) && job.Status == TestJobStatus.Running)
                {
                    long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                    job.Status = TestJobStatus.Failed;
                    job.Error = "Job cleared manually (stuck or orphaned)";
                    job.FinishedUnixMs = now;
                    job.LastUpdateUnixMs = now;
                    McpLog.Warn($"[TestJobManager] Manually cleared stuck job {_currentJobId}");
                    cleared = true;
                }

                _currentJobId = null;
            }
            if (cleared)
            {
                RestoreAfterInterruptedRun();
            }
            PersistToSessionState(force: true);
            return cleared;
        }

        private sealed class PersistedState
        {
            public string current_job_id { get; set; }
            public List<PersistedJob> jobs { get; set; }
        }

        private sealed class PersistedJob
        {
            public string job_id { get; set; }
            public string status { get; set; }
            public string mode { get; set; }
            public long started_unix_ms { get; set; }
            public long? finished_unix_ms { get; set; }
            public long last_update_unix_ms { get; set; }
            public int? total_tests { get; set; }
            public int completed_tests { get; set; }
            public string current_test_full_name { get; set; }
            public long? current_test_started_unix_ms { get; set; }
            public string last_finished_test_full_name { get; set; }
            public long? last_finished_unix_ms { get; set; }
            public List<TestJobFailure> failures_so_far { get; set; }
            public string error { get; set; }
            public long init_timeout_ms { get; set; }
            public string run_guid { get; set; }
            public string phase { get; set; }
            public bool run_started_observed { get; set; }
        }

        private static TestJobStatus ParseStatus(string status)
        {
            if (string.IsNullOrWhiteSpace(status))
            {
                return TestJobStatus.Running;
            }

            string s = status.Trim().ToLowerInvariant();
            return s switch
            {
                "succeeded" => TestJobStatus.Succeeded,
                "failed" => TestJobStatus.Failed,
                "cancelled" => TestJobStatus.Cancelled,
                _ => TestJobStatus.Running
            };
        }

        private static void TryRestoreFromSessionState()
        {
            try
            {
                string json = SessionState.GetString(SessionKeyJobs, string.Empty);
                if (string.IsNullOrWhiteSpace(json))
                {
                    var legacy = SessionState.GetString(SessionKeyCurrentJobId, string.Empty);
                    _currentJobId = string.IsNullOrWhiteSpace(legacy) ? null : legacy;
                    return;
                }

                var state = JsonConvert.DeserializeObject<PersistedState>(json);
                if (state?.jobs == null)
                {
                    return;
                }

                bool interruptedBeforeStart = false;
                lock (LockObj)
                {
                    Jobs.Clear();
                    foreach (var pj in state.jobs)
                    {
                        if (pj == null || string.IsNullOrWhiteSpace(pj.job_id))
                        {
                            continue;
                        }

                        Jobs[pj.job_id] = new TestJob
                        {
                            JobId = pj.job_id,
                            Status = ParseStatus(pj.status),
                            Mode = pj.mode,
                            StartedUnixMs = pj.started_unix_ms,
                            FinishedUnixMs = pj.finished_unix_ms,
                            LastUpdateUnixMs = pj.last_update_unix_ms,
                            TotalTests = pj.total_tests,
                            CompletedTests = pj.completed_tests,
                            CurrentTestFullName = pj.current_test_full_name,
                            CurrentTestStartedUnixMs = pj.current_test_started_unix_ms,
                            LastFinishedTestFullName = pj.last_finished_test_full_name,
                            LastFinishedUnixMs = pj.last_finished_unix_ms,
                            FailuresSoFar = pj.failures_so_far ?? new List<TestJobFailure>(),
                            Error = pj.error,
                            InitTimeoutMs = pj.init_timeout_ms,
                            RunGuid = pj.run_guid,
                            Phase = string.IsNullOrWhiteSpace(pj.phase) ? "initializing" : pj.phase,
                            RunStartedObserved = pj.run_started_observed,
                            // Intentionally not persisted to avoid ballooning SessionState.
                            Result = null
                        };
                    }

                    _currentJobId = string.IsNullOrWhiteSpace(state.current_job_id) ? null : state.current_job_id;
                    if (!string.IsNullOrEmpty(_currentJobId) && !Jobs.ContainsKey(_currentJobId))
                    {
                        _currentJobId = null;
                    }

                    // Detect and clean up stale "running" jobs that were orphaned by domain reload.
                    // After a domain reload, TestRunStatus resets to not-running, but _currentJobId
                    // may still be set. If the job hasn't been updated recently, it's likely orphaned.
                    if (!string.IsNullOrEmpty(_currentJobId) && Jobs.TryGetValue(_currentJobId, out var currentJob))
                    {
                        if (currentJob.Status == TestJobStatus.Running)
                        {
                            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                            if (!currentJob.RunStartedObserved)
                            {
                                currentJob.Status = TestJobStatus.Failed;
                                currentJob.Error = "Test initialization was interrupted by assembly reload";
                                currentJob.Phase = "orphaned";
                                currentJob.FinishedUnixMs = now;
                                currentJob.LastUpdateUnixMs = now;
                                _currentJobId = null;
                                interruptedBeforeStart = true;
                            }
                            long staleCutoffMs = 5 * 60 * 1000; // 5 minutes
                            if (!interruptedBeforeStart && now - currentJob.LastUpdateUnixMs > staleCutoffMs)
                            {
                                McpLog.Warn($"[TestJobManager] Clearing stale job {_currentJobId} (last update {(now - currentJob.LastUpdateUnixMs) / 1000}s ago)");
                                currentJob.Status = TestJobStatus.Failed;
                                currentJob.Error = "Job orphaned after domain reload";
                                currentJob.Phase = "orphaned";
                                currentJob.FinishedUnixMs = now;
                                _currentJobId = null;
                            }
                        }
                    }
                }
                if (interruptedBeforeStart)
                {
                    RestoreAfterInterruptedRun();
                    PersistToSessionState(force: true);
                }
            }
            catch (Exception ex)
            {
                // Restoration is best-effort; never block editor load.
                McpLog.Warn($"[TestJobManager] Failed to restore SessionState: {ex.Message}");
            }
        }

        private static void PersistToSessionState(bool force = false)
        {
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            
            // Throttle non-critical updates to reduce overhead during large test runs
            if (!force && (now - _lastPersistUnixMs) < MinPersistIntervalMs)
            {
                return;
            }
            
            try
            {
                PersistedState snapshot;
                lock (LockObj)
                {
                    var jobs = Jobs.Values
                        .OrderByDescending(j => j.LastUpdateUnixMs)
                        .Take(MaxJobsToKeep)
                        .Select(j => new PersistedJob
                        {
                            job_id = j.JobId,
                            status = j.Status.ToString().ToLowerInvariant(),
                            mode = j.Mode,
                            started_unix_ms = j.StartedUnixMs,
                            finished_unix_ms = j.FinishedUnixMs,
                            last_update_unix_ms = j.LastUpdateUnixMs,
                            total_tests = j.TotalTests,
                            completed_tests = j.CompletedTests,
                            current_test_full_name = j.CurrentTestFullName,
                            current_test_started_unix_ms = j.CurrentTestStartedUnixMs,
                            last_finished_test_full_name = j.LastFinishedTestFullName,
                            last_finished_unix_ms = j.LastFinishedUnixMs,
                            failures_so_far = (j.FailuresSoFar ?? new List<TestJobFailure>()).Take(FailureCap).ToList(),
                            error = j.Error,
                            init_timeout_ms = j.InitTimeoutMs,
                            run_guid = j.RunGuid,
                            phase = j.Phase,
                            run_started_observed = j.RunStartedObserved
                        })
                        .ToList();

                    snapshot = new PersistedState
                    {
                        current_job_id = _currentJobId,
                        jobs = jobs
                    };
                }

                SessionState.SetString(SessionKeyCurrentJobId, snapshot.current_job_id ?? string.Empty);
                SessionState.SetString(SessionKeyJobs, JsonConvert.SerializeObject(snapshot));
                _lastPersistUnixMs = now;
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[TestJobManager] Failed to persist SessionState: {ex.Message}");
            }
        }

        public static string StartJob(TestMode mode, TestFilterOptions filterOptions = null, long initTimeoutMs = 0)
        {
            // Clamp to valid range: non-positive values mean "use default", cap at 10 minutes
            if (initTimeoutMs < 0) initTimeoutMs = 0;
            if (initTimeoutMs > MaxInitializationTimeoutMs) initTimeoutMs = MaxInitializationTimeoutMs;

            string jobId = Guid.NewGuid().ToString("N");
            long started = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            string modeStr = mode.ToString();

            var job = new TestJob
            {
                JobId = jobId,
                Status = TestJobStatus.Running,
                Mode = modeStr,
                StartedUnixMs = started,
                FinishedUnixMs = null,
                LastUpdateUnixMs = started,
                TotalTests = null,
                CompletedTests = 0,
                CurrentTestFullName = null,
                CurrentTestStartedUnixMs = null,
                LastFinishedTestFullName = null,
                LastFinishedUnixMs = null,
                FailuresSoFar = new List<TestJobFailure>(),
                Error = null,
                Result = null,
                InitTimeoutMs = initTimeoutMs,
                RunGuid = null,
                Phase = "initializing",
                RunStartedObserved = false
            };

            // Single lock scope for check-and-set to avoid TOCTOU race
            lock (LockObj)
            {
                if (!string.IsNullOrEmpty(_currentJobId))
                {
                    throw new InvalidOperationException("A Unity test run is already in progress.");
                }
                Jobs[jobId] = job;
                _currentJobId = jobId;
            }
            PersistToSessionState(force: true);

            // Kick the run (must be called on main thread; our command handlers already run there).
            Task<TestRunResult> task = MCPServiceLocator.Tests.RunTestsAsync(mode, filterOptions);

            void FinalizeJob(Action finalize)
            {
                // Ensure state mutation happens on main thread to avoid Unity API surprises.
                EditorApplication.delayCall += () =>
                {
                    try { finalize(); }
                    catch (Exception ex) { McpLog.Error($"[TestJobManager] Finalize failed: {ex.Message}\n{ex.StackTrace}"); }
                };
            }

            task.ContinueWith(t =>
            {
                // NOTE: We now finalize jobs deterministically from the TestRunnerService RunFinished callback.
                // This continuation is retained as a safety net in case RunFinished is not delivered.
                FinalizeJob(() => FinalizeFromTask(jobId, t));
            }, TaskScheduler.Default);

            return jobId;
        }

        public static void FinalizeCurrentJobFromRunFinished(TestRunResult resultPayload)
        {
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId) || !Jobs.TryGetValue(_currentJobId, out var job))
                {
                    return;
                }

                job.LastUpdateUnixMs = now;
                job.FinishedUnixMs = now;
                bool wasCancelling = string.Equals(job.Phase, "cancelling", StringComparison.Ordinal);
                job.Status = wasCancelling
                    ? TestJobStatus.Cancelled
                    : resultPayload != null && resultPayload.Failed > 0
                        ? TestJobStatus.Failed
                        : TestJobStatus.Succeeded;
                job.Error = null;
                job.Phase = wasCancelling ? "cancelled" : "completed";
                job.Result = resultPayload;
                job.CurrentTestFullName = null;
                _currentJobId = null;
            }
            PersistToSessionState(force: true);
        }

        public static void OnRunStarted(int? totalTests)
        {
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId) || !Jobs.TryGetValue(_currentJobId, out var job))
                {
                    return;
                }

                job.LastUpdateUnixMs = now;
                job.TotalTests = totalTests;
                job.RunStartedObserved = true;
                job.Phase = "running";
                job.CompletedTests = 0;
                job.CurrentTestFullName = null;
                job.CurrentTestStartedUnixMs = null;
                job.LastFinishedTestFullName = null;
                job.LastFinishedUnixMs = null;
                job.FailuresSoFar ??= new List<TestJobFailure>();
                job.FailuresSoFar.Clear();
            }
            PersistToSessionState(force: true);
        }

        public static void OnTestStarted(string testFullName)
        {
            if (string.IsNullOrWhiteSpace(testFullName))
            {
                return;
            }

            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId) || !Jobs.TryGetValue(_currentJobId, out var job))
                {
                    return;
                }

                job.LastUpdateUnixMs = now;
                job.CurrentTestFullName = testFullName;
                job.CurrentTestStartedUnixMs = now;
            }
            PersistToSessionState();
        }

        public static void OnLeafTestFinished(string testFullName, bool isFailure, string message)
        {
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId) || !Jobs.TryGetValue(_currentJobId, out var job))
                {
                    return;
                }

                job.LastUpdateUnixMs = now;
                job.CompletedTests = Math.Max(0, job.CompletedTests + 1);
                job.LastFinishedTestFullName = testFullName;
                job.LastFinishedUnixMs = now;

                if (isFailure)
                {
                    job.FailuresSoFar ??= new List<TestJobFailure>();
                    if (job.FailuresSoFar.Count < FailureCap)
                    {
                        job.FailuresSoFar.Add(new TestJobFailure
                        {
                            FullName = testFullName,
                            Message = string.IsNullOrWhiteSpace(message) ? "Test failed" : message
                        });
                    }
                }
            }
            PersistToSessionState();
        }

        public static void OnRunFinished()
        {
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId) || !Jobs.TryGetValue(_currentJobId, out var job))
                {
                    return;
                }

                job.LastUpdateUnixMs = now;
                job.CurrentTestFullName = null;
            }
            PersistToSessionState(force: true);
        }

        internal static TestJob GetJob(string jobId)
        {
            if (string.IsNullOrWhiteSpace(jobId))
            {
                return null;
            }

            TestJob jobToReturn = null;
            bool shouldPersist = false;
            lock (LockObj)
            {
                if (!Jobs.TryGetValue(jobId, out var job))
                {
                    return null;
                }

                // Check if job is stuck in "running" state without having called OnRunStarted (TotalTests still null).
                // This happens when tests fail to initialize (e.g., unsaved scene, compilation issues).
                // After 15 seconds without initialization, auto-fail the job to prevent hanging.
                if (job.Status == TestJobStatus.Running && job.TotalTests == null)
                {
                    long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                    long initTimeout = job.InitTimeoutMs > 0 ? job.InitTimeoutMs : DefaultInitializationTimeoutMs;
                    if (!EditorApplication.isCompiling && !EditorApplication.isUpdating && now - job.StartedUnixMs > initTimeout)
                    {
                        McpLog.Warn($"[TestJobManager] Job {jobId} failed to initialize within {initTimeout}ms, auto-failing");
                        job.Status = TestJobStatus.Failed;
                        job.Error = "Test job failed to initialize (tests did not start within timeout)";
                        job.Phase = "orphaned";
                        job.FinishedUnixMs = now;
                        job.LastUpdateUnixMs = now;
                        if (_currentJobId == jobId)
                        {
                            _currentJobId = null;
                            // Keep TestRunStatus in sync: when initialization times out, neither
                            // RunStarted nor RunFinished fires, so the running flag would otherwise leak.
                            // Only clear it if this job is still the active one — a newer job may have taken over.
                            TestRunStatus.MarkFinished();
                        }
                        shouldPersist = true;
                    }
                }

                jobToReturn = job;
            }

            if (shouldPersist)
            {
                PersistToSessionState(force: true);
            }
            return jobToReturn;
        }

        internal static object ToSerializable(TestJob job, bool includeDetails, bool includeFailedTests)
        {
            if (job == null)
            {
                return null;
            }

            object resultPayload = null;
            if (job.Status == TestJobStatus.Succeeded && job.Result != null)
            {
                resultPayload = job.Result.ToSerializable(job.Mode, includeDetails, includeFailedTests);
            }

            return new
            {
                job_id = job.JobId,
                status = job.Status.ToString().ToLowerInvariant(),
                mode = job.Mode,
                phase = job.Phase,
                run_guid = job.RunGuid,
                started_unix_ms = job.StartedUnixMs,
                finished_unix_ms = job.FinishedUnixMs,
                last_update_unix_ms = job.LastUpdateUnixMs,
                progress = new
                {
                    completed = job.CompletedTests,
                    total = job.TotalTests,
                    current_test_full_name = job.CurrentTestFullName,
                    current_test_started_unix_ms = job.CurrentTestStartedUnixMs,
                    last_finished_test_full_name = job.LastFinishedTestFullName,
                    last_finished_unix_ms = job.LastFinishedUnixMs,
                    stuck_suspected = IsStuck(job),
                    editor_is_focused = InternalEditorUtility.isApplicationActive,
                    blocked_reason = GetBlockedReason(job),
                    failures_so_far = BuildFailuresPayload(job.FailuresSoFar),
                    failures_capped = (job.FailuresSoFar != null && job.FailuresSoFar.Count >= FailureCap)
                },
                error = job.Error,
                result = resultPayload
            };
        }

        private static string GetBlockedReason(TestJob job)
        {
            if (job == null || job.Status != TestJobStatus.Running)
            {
                return null;
            }

            if (!IsStuck(job))
            {
                return null;
            }

            // This matches the real-world symptom you observed: background Unity can get heavily throttled by OS/Editor.
            if (!InternalEditorUtility.isApplicationActive)
            {
                return "editor_unfocused";
            }

            if (EditorApplication.isCompiling)
            {
                return "compiling";
            }

            if (EditorApplication.isUpdating)
            {
                return "asset_import";
            }

            return "unknown";
        }

        private static bool IsStuck(TestJob job)
        {
            if (job == null || job.Status != TestJobStatus.Running)
            {
                return false;
            }

            if (string.IsNullOrWhiteSpace(job.CurrentTestFullName) || !job.CurrentTestStartedUnixMs.HasValue)
            {
                return false;
            }

            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            return (now - job.CurrentTestStartedUnixMs.Value) > StuckThresholdMs;
        }

        private static object[] BuildFailuresPayload(List<TestJobFailure> failures)
        {
            if (failures == null || failures.Count == 0)
            {
                return Array.Empty<object>();
            }

            var list = new object[failures.Count];
            for (int i = 0; i < failures.Count; i++)
            {
                var f = failures[i];
                list[i] = new { full_name = f?.FullName, message = f?.Message };
            }
            return list;
        }

        private static void FinalizeFromTask(string jobId, Task<TestRunResult> task)
        {
            lock (LockObj)
            {
                if (!Jobs.TryGetValue(jobId, out var existing))
                {
                    if (_currentJobId == jobId) _currentJobId = null;
                    return;
                }

                // If RunFinished already finalized the job, do nothing.
                if (existing.Status != TestJobStatus.Running)
                {
                    if (_currentJobId == jobId) _currentJobId = null;
                    return;
                }

                existing.LastUpdateUnixMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                existing.FinishedUnixMs = existing.LastUpdateUnixMs;

                if (task.IsFaulted)
                {
                    existing.Status = TestJobStatus.Failed;
                    existing.Error = task.Exception?.GetBaseException()?.Message ?? "Unknown test job failure";
                    existing.Phase = "completed";
                    existing.Result = null;
                }
                else if (task.IsCanceled)
                {
                    existing.Status = TestJobStatus.Cancelled;
                    existing.Error = "Test job canceled";
                    existing.Phase = "cancelled";
                    existing.Result = null;
                }
                else
                {
                    var result = task.Result;
                    existing.Status = result != null && result.Failed > 0
                        ? TestJobStatus.Failed
                        : TestJobStatus.Succeeded;
                    existing.Error = null;
                    existing.Phase = "completed";
                    existing.Result = result;
                }

                if (_currentJobId == jobId)
                {
                    _currentJobId = null;
                }
            }
            PersistToSessionState(force: true);
        }

        public static void OnRunGuidAssigned(string runGuid)
        {
            if (string.IsNullOrWhiteSpace(runGuid))
            {
                return;
            }

            lock (LockObj)
            {
                if (string.IsNullOrEmpty(_currentJobId) || !Jobs.TryGetValue(_currentJobId, out var job))
                {
                    return;
                }

                job.RunGuid = runGuid;
                job.LastUpdateUnixMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            }
            PersistToSessionState(force: true);
        }

        public static bool CancelJob(string jobId, out string error)
        {
            error = null;
            TestJob job;
            lock (LockObj)
            {
                if (string.IsNullOrWhiteSpace(jobId) || !Jobs.TryGetValue(jobId, out job))
                {
                    error = "Unknown job_id.";
                    return false;
                }
                if (job.Status != TestJobStatus.Running)
                {
                    error = "Test job is not running.";
                    return false;
                }
            }

            bool requested = !string.IsNullOrWhiteSpace(job.RunGuid)
                && TryCancelTestRun(job.RunGuid);
            if (requested)
            {
                lock (LockObj)
                {
                    job.Phase = "cancelling";
                    job.LastUpdateUnixMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                }
                PersistToSessionState(force: true);
                return true;
            }

            if (!TestRunStatus.IsRunning && !job.RunStartedObserved)
            {
                long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                lock (LockObj)
                {
                    job.Status = TestJobStatus.Cancelled;
                    job.Error = "Orphaned test initialization cancelled";
                    job.Phase = "cancelled";
                    job.FinishedUnixMs = now;
                    job.LastUpdateUnixMs = now;
                    if (_currentJobId == jobId)
                    {
                        _currentJobId = null;
                    }
                }
                RestoreAfterInterruptedRun();
                PersistToSessionState(force: true);
                return true;
            }

            error = "Unity Test Runner did not accept the cancellation request.";
            return false;
        }

        private static bool TryCancelTestRun(string runGuid)
        {
            // Test Framework 1.1 exposes cancellation differently from newer
            // package versions. Reflection keeps the package compatible with the
            // Unity 2021+ support matrix while still using the GUID overload when
            // it is available.
            MethodInfo method = typeof(TestRunnerApi).GetMethod(
                "CancelTestRun",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                null,
                new[] { typeof(string) },
                null);
            object[] arguments = { runGuid };
            if (method == null)
            {
                method = typeof(TestRunnerApi).GetMethod(
                    "CancelTestRun",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                    null,
                    Type.EmptyTypes,
                    null);
                arguments = null;
            }
            if (method == null)
            {
                return false;
            }

            try
            {
                object result = method.Invoke(null, arguments);
                return !(result is bool accepted) || accepted;
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[TestJobManager] Test cancellation failed: {ex.GetBaseException().Message}");
                return false;
            }
        }

        private static void RestoreAfterInterruptedRun()
        {
            TestRunStatus.MarkFinished();
            TestRunnerNoThrottle.RestoreAfterInterruptedRun();
            if (PlayModeOptionsGuard.IsPending)
            {
                PlayModeOptionsGuard.Restore();
            }
            EditorStateCache.ForceUpdate("test_run_interrupted");
        }
    }
}


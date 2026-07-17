using System;
using UnityEditor;
using UnityEditor.Compilation;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Tracks compilation requests made by MCP before Unity reports isCompiling.
    /// Unity can defer a request for several editor ticks, so isCompiling alone is
    /// not a safe readiness signal for starting PlayMode tests.
    /// </summary>
    [InitializeOnLoad]
    internal static class CompilationRequestTracker
    {
        private const string PendingKey = "MCPForUnity.CompilationRequest.Pending";
        private const string RequestedGenerationKey = "MCPForUnity.CompilationRequest.RequestedGeneration";
        private const string CompletedGenerationKey = "MCPForUnity.CompilationRequest.CompletedGeneration";
        private const string RequestedUnixMsKey = "MCPForUnity.CompilationRequest.RequestedUnixMs";
        private const string CompletedUnixMsKey = "MCPForUnity.CompilationRequest.CompletedUnixMs";

        static CompilationRequestTracker()
        {
            CompilationPipeline.compilationFinished += _ => MarkCompleted();
            AssemblyReloadEvents.afterAssemblyReload += CompleteAfterReload;
        }

        internal static bool IsPending => SessionState.GetBool(PendingKey, false);
        internal static int RequestedGeneration => SessionState.GetInt(RequestedGenerationKey, 0);
        internal static int CompletedGeneration => SessionState.GetInt(CompletedGenerationKey, 0);
        internal static long? RequestedUnixMs => ReadLong(RequestedUnixMsKey);
        internal static long? CompletedUnixMs => ReadLong(CompletedUnixMsKey);

        internal static void MarkRequested()
        {
            int generation = RequestedGeneration + 1;
            SessionState.SetInt(RequestedGenerationKey, generation);
            SessionState.SetBool(PendingKey, true);
            WriteLong(RequestedUnixMsKey, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
            EditorStateCache.ForceUpdate("compilation_requested");
        }

        private static void CompleteAfterReload()
        {
            if (IsPending)
            {
                MarkCompleted();
            }
        }

        private static void MarkCompleted()
        {
            if (!IsPending && CompletedGeneration >= RequestedGeneration)
            {
                return;
            }

            SessionState.SetInt(CompletedGenerationKey, RequestedGeneration);
            SessionState.SetBool(PendingKey, false);
            WriteLong(CompletedUnixMsKey, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
            EditorStateCache.ForceUpdate("compilation_completed");
        }

        private static long? ReadLong(string key)
        {
            string value = SessionState.GetString(key, string.Empty);
            return long.TryParse(value, out long parsed) ? parsed : null;
        }

        private static void WriteLong(string key, long value)
        {
            SessionState.SetString(key, value.ToString());
        }
    }
}

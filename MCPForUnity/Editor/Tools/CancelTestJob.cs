using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using Newtonsoft.Json.Linq;

namespace MCPForUnity.Editor.Tools
{
    /// <summary>
    /// Cancels an MCP-owned Unity Test Runner job and reconciles orphaned initialization state.
    /// </summary>
    [McpForUnityTool("cancel_test_job", AutoRegister = false, Group = "testing")]
    public static class CancelTestJob
    {
        public static object HandleCommand(JObject @params)
        {
            string jobId = @params?["job_id"]?.ToString() ?? @params?["jobId"]?.ToString();
            if (string.IsNullOrWhiteSpace(jobId))
            {
                return new ErrorResponse("Missing required parameter 'job_id'.");
            }

            if (!TestJobManager.CancelJob(jobId, out string error))
            {
                return new ErrorResponse(error ?? "Failed to cancel test job.");
            }

            TestJob job = TestJobManager.GetJob(jobId);
            string status = job?.Status.ToString().ToLowerInvariant() ?? "cancelling";

            return new SuccessResponse(
                "Test job cancellation requested.",
                new { job_id = jobId, status });
        }
    }
}

using System;
using System.IO;
using System.Text;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace MCPForUnityTests.Editor.Tools
{
    public class ManageScriptHashlineTextTests
    {
        private const string RelativeDirectory = "Assets/TempHashlineTests";
        private string _fullDirectory;

        [SetUp]
        public void SetUp()
        {
            _fullDirectory = Path.Combine(Application.dataPath, "TempHashlineTests");
            Directory.CreateDirectory(_fullDirectory);
        }

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(_fullDirectory)) Directory.Delete(_fullDirectory, true);
            string meta = _fullDirectory + ".meta";
            if (File.Exists(meta)) File.Delete(meta);
            AssetDatabase.Refresh();
        }

        [Test]
        public void ReadText_ReturnsMetadataForNonCSharpUtf8BomFile()
        {
            string relativePath = RelativeDirectory + "/settings.json";
            string fullPath = Path.Combine(_fullDirectory, "settings.json");
            var encoding = new UTF8Encoding(true);
            File.WriteAllText(fullPath, "{\r\n  \"enabled\": true\r\n}\r\n", encoding);

            JObject result = Invoke(new JObject { ["action"] = "read_text", ["file"] = relativePath });

            Assert.IsTrue(result.Value<bool>("success"));
            Assert.AreEqual("utf-8", result["data"]?["encoding"]?.ToString());
            Assert.AreEqual("crlf", result["data"]?["newline"]?.ToString());
            Assert.IsTrue(result["data"]?.Value<bool>("hasBom") ?? false);
            Assert.IsNotEmpty(result["data"]?["sha256"]?.ToString());
        }

        [Test]
        public void ReadText_RejectsBinaryAndProjectRootEscape()
        {
            string relativePath = RelativeDirectory + "/binary.dat";
            File.WriteAllBytes(Path.Combine(_fullDirectory, "binary.dat"), new byte[] { 1, 0, 2, 3 });

            JObject binary = Invoke(new JObject { ["action"] = "read_text", ["file"] = relativePath });
            JObject escape = Invoke(new JObject { ["action"] = "read_text", ["file"] = "../outside.txt" });

            Assert.IsFalse(binary.Value<bool>("success"));
            Assert.AreEqual("E_BINARY", binary.Value<string>("code"));
            Assert.IsFalse(escape.Value<bool>("success"));
            Assert.AreEqual("E_PATH", escape.Value<string>("code"));
        }

        [Test]
        public void ApplyHashlineText_PreservesEncodingAndNewlinesAndReturnsNewSha()
        {
            string relativePath = RelativeDirectory + "/notes.txt";
            string fullPath = Path.Combine(_fullDirectory, "notes.txt");
            File.WriteAllText(fullPath, "one\r\ntwo\r\n", new UTF8Encoding(true));
            JObject read = Invoke(new JObject { ["action"] = "read_text", ["file"] = relativePath });
            string beforeSha = read["data"]?["sha256"]?.ToString();
            string replacement = Convert.ToBase64String(Encoding.UTF8.GetBytes("one\r\nTWO\r\n"));

            JObject result = Invoke(new JObject
            {
                ["action"] = "apply_hashline_edits",
                ["file"] = relativePath,
                ["encodedContents"] = replacement,
                ["contentsEncoded"] = true,
                ["precondition_sha256"] = beforeSha,
                ["options"] = new JObject { ["refresh"] = "immediate" },
            });

            byte[] bytes = File.ReadAllBytes(fullPath);
            Assert.IsTrue(result.Value<bool>("success"));
            Assert.AreNotEqual(beforeSha, result["data"]?["sha256"]?.ToString());
            Assert.AreEqual(0xEF, bytes[0]);
            Assert.AreEqual(0xBB, bytes[1]);
            Assert.AreEqual(0xBF, bytes[2]);
            Assert.AreEqual("one\r\nTWO\r\n", File.ReadAllText(fullPath));
        }

        [Test]
        public void ApplyHashlineText_StalePreconditionDoesNotModifyFile()
        {
            string relativePath = RelativeDirectory + "/stale.txt";
            string fullPath = Path.Combine(_fullDirectory, "stale.txt");
            File.WriteAllText(fullPath, "original\n", new UTF8Encoding(false));
            string replacement = Convert.ToBase64String(Encoding.UTF8.GetBytes("changed\n"));

            JObject result = Invoke(new JObject
            {
                ["action"] = "apply_hashline_edits",
                ["file"] = relativePath,
                ["encodedContents"] = replacement,
                ["contentsEncoded"] = true,
                ["precondition_sha256"] = new string('0', 64),
            });

            Assert.IsFalse(result.Value<bool>("success"));
            Assert.AreEqual("E_PRECONDITION", result.Value<string>("code"));
            Assert.AreEqual("original\n", File.ReadAllText(fullPath));
        }

        [Test]
        public void ApplyHashlineText_AllowsEmptyReplacementForWholeFileDeletion()
        {
            string relativePath = RelativeDirectory + "/delete-all.txt";
            string fullPath = Path.Combine(_fullDirectory, "delete-all.txt");
            File.WriteAllText(fullPath, "remove me\n", new UTF8Encoding(false));
            JObject read = Invoke(new JObject { ["action"] = "read_text", ["file"] = relativePath });

            JObject result = Invoke(new JObject
            {
                ["action"] = "apply_hashline_edits",
                ["file"] = relativePath,
                ["encodedContents"] = string.Empty,
                ["contentsEncoded"] = true,
                ["precondition_sha256"] = read["data"]?["sha256"]?.ToString(),
                ["options"] = new JObject { ["refresh"] = "immediate" },
            });

            Assert.IsTrue(result.Value<bool>("success"));
            Assert.AreEqual(0, new FileInfo(fullPath).Length);
        }

        private static JObject Invoke(JObject parameters)
        {
            return JObject.FromObject(ManageScript.HandleCommand(parameters));
        }
    }
}

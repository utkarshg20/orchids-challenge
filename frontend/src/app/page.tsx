// frontend/app/page.tsx

"use client";

import { useState, useEffect } from "react";

export default function Page() {
  const [url, setUrl] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<"queued" | "running" | "complete" | "error">("queued");
  const [progress, setProgress] = useState<number>(0);
  const [errorDetail, setErrorDetail] = useState("");
  const [overlayOpen, setOverlayOpen] = useState(false);
  const [showCode, setShowCode] = useState(false);
  const [htmlCode, setHtmlCode] = useState("");

  // Poll backend for status updates
  useEffect(() => {
    if (!jobId) return;
    const interval = setInterval(async () => {
      const res = await fetch(`http://localhost:8000/jobs/${jobId}`);
      if (!res.ok) {
        setErrorDetail("Job not found");
        setStatus("error");
        clearInterval(interval);
        return;
      }
      const data = await res.json();
      setStatus(data.status as any);
      setProgress(parseInt(data.progress, 10));
      if (data.status === "error") {
        setErrorDetail(data.detail);
        clearInterval(interval);
      }
      if (data.status === "complete") {
        clearInterval(interval);
        setOverlayOpen(true);
      }
    }, 1000);
    return () => clearInterval(interval);
  }, [jobId]);

  // Handle form submission and URL validation
  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    try {
      new URL(url);
      setErrorDetail("");
    } catch {
      setErrorDetail("Please enter a valid URL, e.g. https://example.com");
      return;
    }
    setStatus("running");
    setProgress(0);

    const response = await fetch("http://localhost:8000/clone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!response.ok) {
      setStatus("error");
      setErrorDetail("Failed to enqueue clone job.");
      return;
    }
    const data = await response.json();
    setJobId(data.job_id);
  }

  // Fetch HTML code when requested
  async function onViewCode() {
    if (!jobId) return;
    try {
      const res = await fetch(`http://localhost:8000/clone/${jobId}/raw`);
      if (res.ok) {
        const text = await res.text();
        setHtmlCode(text);
        setShowCode(true);
      } else {
        setHtmlCode("Could not fetch HTML code.");
        setShowCode(true);
      }
    } catch {
      setHtmlCode("Error fetching HTML code.");
      setShowCode(true);
    }
  }

  // Close overlay and reset to default view
  function closeOverlay() {
    setOverlayOpen(false);
    setShowCode(false);
  }

  return (
    <div className="min-h-screen bg-gray-100 flex flex-col items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
      {/* Entry Card */}
      <div className="w-full max-w-md bg-white shadow-xl rounded-lg p-6">
        <h1 className="text-center text-3xl font-extrabold text-gray-900 mb-4">
          Orchids Website Cloner
        </h1>
        <p className="text-center text-gray-600 mb-6">
          Enter any public website URL below, and we’ll generate a cloned HTML preview.
        </p>
        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <label htmlFor="url" className="sr-only">
              Website URL
            </label>
            <input
              id="url"
              name="url"
              type="text"
              autoComplete="url"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="appearance-none block w-full px-4 py-2 border border-gray-300 rounded-md shadow-sm placeholder-gray-500 text-gray-900 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm bg-gray-50"
              placeholder="https://example.com"
            />
          </div>
          <button
            type="submit"
            className="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-md text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
          >
            {status === "running" ? "Cloning..." : "Clone Website"}
          </button>
        </form>
        {errorDetail && (
          <p className="mt-4 text-sm text-red-600 text-center">{errorDetail}</p>
        )}

        {status === "running" && (
          <div className="mt-6">
            <p className="text-sm text-gray-700 mb-2">Progress: {progress}%</p>
            <div className="w-full bg-gray-200 rounded-full h-3">
              <div
                className="bg-indigo-600 h-3 rounded-full transition-all duration-500"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        )}
      </div>

      {/* Full-Screen Overlay Modal */}
      {overlayOpen && jobId && (
        <div className="fixed inset-0 z-50 flex flex-col bg-gray-900 bg-opacity-80">
          {/* Top Bar */}
          <div className="flex justify-between items-center bg-white px-6 py-3 shadow-md">
            <h2 className="text-lg font-bold text-gray-800">Cloned Website Preview</h2>
            <button
              onClick={closeOverlay}
              className="text-gray-600 hover:text-gray-900 text-3xl font-bold"
              aria-label="Close"
            >
              &times;
            </button>
          </div>

          {/* Tab Buttons */}
          <div className="flex bg-white">
            <button
              onClick={() => setShowCode(false)}
              className={`flex-1 py-3 text-center ${
                !showCode
                  ? "border-b-4 border-indigo-600 text-indigo-600 font-semibold"
                  : "text-gray-600"
              } hover:bg-gray-50`}
            >
              View Site
            </button>
            <button
              onClick={onViewCode}
              className={`flex-1 py-3 text-center ${
                showCode
                  ? "border-b-4 border-indigo-600 text-indigo-600 font-semibold"
                  : "text-gray-600"
              } hover:bg-gray-50`}
            >
              View HTML Code
            </button>
          </div>

          {/* Content Area */}
          <div className="flex-1 overflow-hidden bg-white">
            {!showCode ? (
              <iframe
                title="Cloned Site"
                src={`http://localhost:8000/clone/${jobId}/raw`}
                className="w-full h-full border-none"
              />
            ) : (
              <div className="h-full overflow-auto bg-gray-100 p-4">
                <pre className="text-xs text-gray-800 whitespace-pre-wrap">
                  {htmlCode}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

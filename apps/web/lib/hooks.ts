"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Calls `fn` every `ms` while `ms` is a number; pass null to stop. Pauses while the tab is hidden. */
export function usePolling(fn: () => unknown, ms: number | null) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (ms === null) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") saved.current();
    }, ms);
    return () => window.clearInterval(id);
  }, [ms]);
}

export type Recorder = {
  recording: boolean;
  seconds: number;
  error: string | null;
  start: () => Promise<void>;
  stop: () => void;
};

/** Microphone recording with MediaRecorder. `onDone` gets the finished audio. */
export function useRecorder(onDone: (audio: Blob) => void, maxSeconds = 60): Recorder {
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<number | null>(null);
  const done = useRef(onDone);
  done.current = onDone;

  const stop = useCallback(() => {
    if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
  }, []);

  const start = useCallback(async () => {
    setError(null);
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("Voice isn't supported in this browser — type instead.");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError("Microphone access was blocked. Allow it in the browser's address bar, or type instead.");
      return;
    }
    const preferred = ["audio/webm", "audio/mp4", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported(t));
    const rec = preferred ? new MediaRecorder(stream, { mimeType: preferred }) : new MediaRecorder(stream);
    const chunks: BlobPart[] = [];
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      if (timer.current) window.clearInterval(timer.current);
      setRecording(false);
      const blob = new Blob(chunks, { type: rec.mimeType || preferred || "audio/webm" });
      if (blob.size) done.current(blob);
    };
    recorder.current = rec;
    rec.start();
    setSeconds(0);
    setRecording(true);
    const began = Date.now();
    timer.current = window.setInterval(() => {
      const s = Math.floor((Date.now() - began) / 1000);
      setSeconds(s);
      if (s >= maxSeconds) rec.stop();
    }, 250);
  }, [maxSeconds]);

  useEffect(() => () => {
    if (timer.current) window.clearInterval(timer.current);
    if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
  }, []);

  return { recording, seconds, error, start, stop };
}

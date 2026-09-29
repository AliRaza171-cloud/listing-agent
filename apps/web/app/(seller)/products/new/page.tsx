"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type DragEvent } from "react";
import { CloseIcon, MicIcon, SparkIcon, StopIcon, UploadIcon } from "@/components/Icons";
import { ModeSwitch } from "@/components/ModeSwitch";
import { useRecorder } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { myMarket,
  ApiError, createProduct, generateListing, listStores, mediaUrl, transcribe, uploadPhoto, type Store,
} from "@/lib/api";
import { PLATFORM_NAMES } from "@/lib/format";

const MAX_PHOTOS = 8;
const MAX_MB = 8;
const TYPES = ["image/jpeg", "image/png", "image/webp"];

type Photo = { key: string; preview: string; url: string | null; error: string | null };

export default function NewListingPage() {
  const router = useRouter();
  const { credits, refreshCredits } = useSession();
  const fileInput = useRef<HTMLInputElement>(null);

  const [photos, setPhotos] = useState<Photo[]>([]);
  const [notes, setNotes] = useState("");
  // Urdu is ticked by default for sellers in Pakistan only.
  const [langs, setLangs] = useState(() => ({ en: true, ur: myMarket().country === "PK" }));
  const [stores, setStores] = useState<Store[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [research, setResearch] = useState(true);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [transcribing, setTranscribing] = useState(false);

  useEffect(() => {
    listStores()
      .then((s) => {
        setStores(s);
        setPicked(s.filter((x) => x.status === "active").map((x) => x.id));
      })
      .catch(() => setStores([]));
  }, []);

  // free the preview URLs when leaving the page
  const previews = useRef<string[]>([]);
  useEffect(() => () => previews.current.forEach((u) => URL.revokeObjectURL(u)), []);

  const recorder = useRecorder(async (audio) => {
    setTranscribing(true);
    setError(null);
    try {
      const { text } = await transcribe(audio);
      if (text) setNotes((n) => (n.trim() ? `${n.trim()} ${text}` : text));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't understand the recording.");
    } finally {
      setTranscribing(false);
    }
  });

  function addFiles(list: FileList | File[]) {
    setError(null);
    const files = Array.from(list);
    const room = MAX_PHOTOS - photos.length;
    if (room <= 0) {
      setError(`You can add up to ${MAX_PHOTOS} photos.`);
      return;
    }
    for (const file of files.slice(0, room)) {
      const key = `${file.name}-${file.size}-${Math.random().toString(36).slice(2)}`;
      if (!TYPES.includes(file.type)) {
        setError(`${file.name}: use JPG, PNG or WebP.`);
        continue;
      }
      if (file.size > MAX_MB * 1024 * 1024) {
        setError(`${file.name} is over ${MAX_MB} MB.`);
        continue;
      }
      const preview = URL.createObjectURL(file);
      previews.current.push(preview);
      setPhotos((p) => [...p, { key, preview, url: null, error: null }]);
      uploadPhoto(file)
        .then((url) => setPhotos((p) => p.map((x) => (x.key === key ? { ...x, url } : x))))
        .catch((e) => setPhotos((p) => p.map((x) => (x.key === key
          ? { ...x, error: e instanceof ApiError ? e.message : "Upload failed" } : x))));
    }
    if (files.length > room) setError(`Only the first ${room} photo${room === 1 ? "" : "s"} were added (max ${MAX_PHOTOS}).`);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  }

  const uploading = photos.some((p) => !p.url && !p.error);
  const ready = photos.filter((p) => p.url);
  const languages = (["en", "ur"] as const).filter((l) => langs[l]);
  const chosenStores = (stores ?? []).filter((s) => picked.includes(s.id));
  const canGenerate = !busy && !uploading && languages.length > 0 && (ready.length > 0 || notes.trim().length > 0);

  async function generate() {
    setError(null);
    setBusy(true);
    let productId: string | null = null;
    try {
      const product = await createProduct(ready.map((p) => p.url as string), notes.trim() || null);
      productId = product.id;
      await generateListing(product.id, {
        languages,
        platforms: Array.from(new Set(chosenStores.map((s) => s.platform))),
        research,
        store_connection_ids: chosenStores.map((s) => s.id),
      });
      refreshCredits();
      router.push(`/products/${product.id}`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Couldn't start the listing.";
      if (productId) {
        // The product is saved; let the seller retry from its page.
        router.push(`/products/${productId}?error=${encodeURIComponent(msg)}`);
        return;
      }
      setError(msg);
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>New listing</h1>
          <p>Add photos, tell the agent what you know, and it does the writing.</p>
        </div>
        <ModeSwitch active="one" />
      </div>

      {error && <div className="alert alert-error" role="alert">{error}</div>}

      <div className="two-col">
        <div className="col">
          <section className="card">
            <div className="card-head">
              <h2>Photos</h2>
              <span className="small muted">{photos.length} of {MAX_PHOTOS} · first photo is the cover</span>
            </div>
            <div className={`drop${over ? " over" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }}
              onDragLeave={() => setOver(false)}
              onDrop={onDrop}
              onClick={() => fileInput.current?.click()}>
              <UploadIcon size={30} />
              <strong>Drag photos here, or <button type="button" className="link-btn"
                onClick={(e) => { e.stopPropagation(); fileInput.current?.click(); }}>browse</button></strong>
              <span className="small">JPG, PNG or WebP · plain background works best</span>
              <input ref={fileInput} type="file" accept={TYPES.join(",")} multiple hidden
                onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
            </div>
            {photos.length > 0 && (
              <div className="photos">
                {photos.map((p, i) => (
                  <div key={p.key} className={`photo${i === 0 ? " cover" : ""}`}>
                    <img src={p.url ? mediaUrl(p.url) : p.preview} alt={`Photo ${i + 1}`} />
                    {i === 0 && <span className="tag">Cover</span>}
                    {!p.url && !p.error && <span className="busy"><span className="spinner" /></span>}
                    {p.error && <span className="busy" style={{ color: "var(--bad)", fontSize: 11, padding: 6, textAlign: "center" }}>{p.error}</span>}
                    <button type="button" className="x" aria-label={`Remove photo ${i + 1}`}
                      onClick={() => setPhotos((all) => all.filter((x) => x.key !== p.key))}>
                      <CloseIcon size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card">
            <label htmlFor="notes" style={{ fontSize: 17, fontWeight: 600 }}>What should the agent know?</label>
            <p className="small muted">Brand, size, material, anything a photo can’t show. Type it or say it.</p>
            <div className="notes-wrap">
              <textarea id="notes" className="textarea" rows={4} value={notes} onChange={(e) => setNotes(e.target.value)}
                placeholder="e.g. 1.3 litre, stainless steel, non-stick inside" />
              <button type="button" className={`icon-btn${recorder.recording ? " recording" : ""}`}
                aria-label={recorder.recording ? "Stop recording" : "Speak your details"}
                disabled={transcribing}
                onClick={() => (recorder.recording ? recorder.stop() : recorder.start())}>
                {transcribing ? <span className="spinner" /> : recorder.recording ? <StopIcon size={16} /> : <MicIcon />}
              </button>
            </div>
            {recorder.recording && <span className="small" style={{ color: "var(--bad)" }}>Listening… {recorder.seconds}s — tap to stop</span>}
            {recorder.error && <span className="small" style={{ color: "var(--bad)" }}>{recorder.error}</span>}
          </section>
        </div>

        <div className="col">
          <section className="card">
            <h2>Write the listing in</h2>
            <div style={{ display: "flex", gap: 10 }}>
              <label className="choice"><input type="checkbox" checked={langs.en}
                onChange={(e) => setLangs({ ...langs, en: e.target.checked })} />English</label>
              <label className="choice"><input type="checkbox" checked={langs.ur}
                onChange={(e) => setLangs({ ...langs, ur: e.target.checked })} /><span className="urdu" style={{ lineHeight: 1.4 }}>اردو</span> Urdu</label>
            </div>
          </section>

          <section className="card">
            <h2>Format it for</h2>
            {stores === null && <span className="skeleton" style={{ height: 20 }} />}
            {stores?.length === 0 && (
              <p className="small muted">No stores connected yet — you’ll get one general listing you can publish later.</p>
            )}
            {stores?.map((s) => (
              <label key={s.id} className="check" style={{ justifyContent: "space-between" }}>
                <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <input type="checkbox" checked={picked.includes(s.id)} disabled={s.status !== "active"}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, s.id] : picked.filter((x) => x !== s.id))} />
                  {PLATFORM_NAMES[s.platform]} — {s.name}
                </span>
                <span className="small" style={{ fontWeight: 600, color: s.status === "active" ? "var(--ok)" : "var(--bad)" }}>
                  {s.status === "active" ? "Connected" : "Needs attention"}
                </span>
              </label>
            ))}
            <Link href="/stores" className="small" style={{ fontWeight: 600 }}>+ Connect a store</Link>
          </section>

          <section className="card">
            <label className="switch-row">Research online
              <input type="checkbox" role="switch" className="switch" checked={research}
                onChange={(e) => setResearch(e.target.checked)} />
            </label>
            <p className="small muted">
              Looks up specs for branded items, or the keywords and price range buyers see for generic ones. Sources are
              shown so you can check them.
            </p>
          </section>

          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <button type="button" className="btn btn-primary btn-lg btn-block" disabled={!canGenerate} onClick={generate}>
              {busy ? <span className="spinner" /> : <SparkIcon />}Generate listing
            </button>
            <span className="small muted" style={{ textAlign: "center" }}>
              {uploading ? "Waiting for photos to finish uploading…"
                : !ready.length && !notes.trim() ? "Add a photo or some details to start."
                : !languages.length ? "Pick at least one language."
                : credits !== null ? `Uses 1 credit · you’ll have ${Math.max(credits - 1, 0)} left` : "Uses 1 credit"}
            </span>
          </div>
        </div>
      </div>
    </>
  );
}

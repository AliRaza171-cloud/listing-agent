"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { CloseIcon, SparkIcon, UploadIcon } from "@/components/Icons";
import { ModeSwitch } from "@/components/ModeSwitch";
import { useSession } from "@/lib/session";
import { myMarket,
  ApiError, createBatch, createProduct, deleteProduct, generateListing, listStores, mediaUrl, uploadPhoto, type Store,
} from "@/lib/api";
import { PLATFORM_NAMES } from "@/lib/format";

const MAX_ITEMS = 50;
const MAX_PHOTOS_PER_ITEM = 8;
const MAX_MB = 8;
const TYPES = ["image/jpeg", "image/png", "image/webp"];

type Photo = { key: string; preview: string; url: string | null; error: string | null };
type Item = { key: string; photos: Photo[]; notes: string; selected: boolean };

const uid = () => Math.random().toString(36).slice(2);

export default function BulkPage() {
  const router = useRouter();
  const { credits, refreshCredits } = useSession();
  const fileInput = useRef<HTMLInputElement>(null);
  const previews = useRef<string[]>([]);

  const [items, setItems] = useState<Item[]>([]);
  const [name, setName] = useState(() => `Batch ${new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`);
  // Urdu is ticked by default for sellers in Pakistan only.
  const [langs, setLangs] = useState(() => ({ en: true, ur: myMarket().country === "PK" }));
  const [stores, setStores] = useState<Store[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [research, setResearch] = useState(false);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number; note: string } | null>(null);

  useEffect(() => {
    listStores()
      .then((s) => { setStores(s); setPicked(s.filter((x) => x.status === "active").map((x) => x.id)); })
      .catch(() => setStores([]));
    return () => previews.current.forEach((u) => URL.revokeObjectURL(u));
  }, []);

  function updatePhoto(key: string, patch: Partial<Photo>) {
    setItems((all) => all.map((it) => ({ ...it, photos: it.photos.map((p) => (p.key === key ? { ...p, ...patch } : p)) })));
  }

  function addFiles(list: FileList | File[]) {
    setError(null);
    const files = Array.from(list).filter((f) => TYPES.includes(f.type) && f.size <= MAX_MB * 1024 * 1024);
    const skipped = list.length - files.length;
    const room = MAX_ITEMS - items.length;
    const accepted = files.slice(0, Math.max(room, 0));
    const newItems: Item[] = accepted.map((file) => {
      const preview = URL.createObjectURL(file);
      previews.current.push(preview);
      const photo: Photo = { key: uid(), preview, url: null, error: null };
      uploadPhoto(file)
        .then((url) => updatePhoto(photo.key, { url }))
        .catch((e) => updatePhoto(photo.key, { error: e instanceof ApiError ? e.message : "Upload failed" }));
      return { key: uid(), photos: [photo], notes: "", selected: false };
    });
    setItems((all) => [...all, ...newItems]);
    const msgs = [];
    if (skipped) msgs.push(`${skipped} file${skipped === 1 ? " was" : "s were"} skipped (JPG/PNG/WebP up to ${MAX_MB} MB).`);
    if (files.length > accepted.length) msgs.push(`Only ${MAX_ITEMS} products per batch.`);
    if (msgs.length) setError(msgs.join(" "));
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  }

  const selected = items.filter((i) => i.selected);
  function combine() {
    if (selected.length < 2) return;
    const [first, ...rest] = selected;
    const photos = [...first.photos, ...rest.flatMap((r) => r.photos)].slice(0, MAX_PHOTOS_PER_ITEM);
    const notes = selected.map((s) => s.notes.trim()).filter(Boolean).join(" ");
    const restKeys = new Set(rest.map((r) => r.key));
    setItems((all) => all.filter((i) => !restKeys.has(i.key))
      .map((i) => (i.key === first.key ? { ...i, photos, notes, selected: false } : i)));
  }

  const uploading = items.some((i) => i.photos.some((p) => !p.url && !p.error));
  const ready = items.filter((i) => i.photos.some((p) => p.url));
  const languages = (["en", "ur"] as const).filter((l) => langs[l]);
  const chosenStores = useMemo(() => (stores ?? []).filter((s) => picked.includes(s.id)), [stores, picked]);
  const notEnough = credits !== null && ready.length > credits;
  const canStart = !progress && !uploading && credits !== 0 && ready.length > 0 && languages.length > 0 && name.trim().length > 0;

  async function start() {
    setError(null);
    let batchId: string;
    try {
      batchId = (await createBatch(name.trim())).id;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start the batch.");
      return;
    }
    const total = ready.length;
    const failures: string[] = [];
    // Create every product first, then start them. While any product is still a draft the batch
    // can't be marked complete, so a fast first listing doesn't close the batch early.
    const ids: (string | null)[] = [];
    for (let i = 0; i < total; i++) {
      setProgress({ done: i / 2, total, note: `Adding ${i + 1} of ${total}…` });
      try {
        const item = ready[i];
        ids.push((await createProduct(item.photos.filter((p) => p.url).map((p) => p.url as string),
          item.notes.trim() || null, batchId)).id);
      } catch (e) {
        ids.push(null);
        failures.push(`Product ${i + 1}: ${e instanceof ApiError ? e.message : "couldn't be added"}`);
      }
    }
    const platforms = Array.from(new Set(chosenStores.map((s) => s.platform)));
    let started = 0;
    for (let i = 0; i < total; i++) {
      const id = ids[i];
      if (!id) continue;
      setProgress({ done: total / 2 + i / 2, total, note: `Starting ${i + 1} of ${total}…` });
      try {
        await generateListing(id, {
          languages, platforms, research, store_connection_ids: chosenStores.map((s) => s.id),
        });
        started++;
      } catch (e) {
        if (e instanceof ApiError && e.status === 402) {
          // Out of credits: remove the ones that can't start so they don't sit there unwritten.
          await Promise.all(ids.slice(i).filter(Boolean).map((x) => deleteProduct(x as string).catch(() => {})));
          failures.push(`Out of credits after ${started} product${started === 1 ? "" : "s"} — the rest weren't started.`);
          break;
        }
        await deleteProduct(id).catch(() => {});
        failures.push(`Product ${i + 1}: ${e instanceof ApiError ? e.message : "couldn't start"}`);
      }
    }
    refreshCredits();
    setProgress({ done: total, total, note: "Done" });
    if (failures.length) {
      sessionStorageSafe(`batch-note-${batchId}`, failures.join("\n"));
    }
    router.push(`/products?batch=${batchId}`);
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Bulk upload</h1>
          <p>Drop in many product photos — each becomes its own listing. Combine photos of the same product first.</p>
        </div>
        <ModeSwitch active="bulk" />
      </div>

      {error && <div className="alert alert-error" role="alert" style={{ whiteSpace: "pre-line" }}>{error}</div>}

      <div className="two-col">
        <div className="col">
          <section className="card">
            <div className="card-head">
              <h2>Products</h2>
              <span className="small muted">{items.length} of {MAX_ITEMS}</span>
            </div>
            <div className={`drop${over ? " over" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }}
              onDragLeave={() => setOver(false)} onDrop={onDrop}
              onClick={() => fileInput.current?.click()}>
              <UploadIcon size={30} />
              <strong>Drag photos here, or <button type="button" className="link-btn"
                onClick={(e) => { e.stopPropagation(); fileInput.current?.click(); }}>browse</button></strong>
              <span className="small">One photo = one product. Several photos of one product? Tick them and press “Combine”.</span>
              <input ref={fileInput} type="file" accept={TYPES.join(",")} multiple hidden
                onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
            </div>

            {items.length > 0 && (
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <button type="button" className="btn btn-ghost btn-sm" disabled={selected.length < 2} onClick={combine}>
                  Combine {selected.length >= 2 ? selected.length : ""} into one product
                </button>
                <button type="button" className="btn btn-danger btn-sm" disabled={!selected.length}
                  onClick={() => setItems((all) => all.filter((i) => !i.selected))}>Remove selected</button>
              </div>
            )}

            <div className="bulk-list">
              {items.map((it, idx) => (
                <div key={it.key} className={`bulk-item${it.selected ? " selected" : ""}`}>
                  <label className="check" style={{ alignSelf: "flex-start" }}>
                    <input type="checkbox" checked={it.selected} aria-label={`Select product ${idx + 1}`}
                      onChange={(e) => setItems((all) => all.map((x) => (x.key === it.key ? { ...x, selected: e.target.checked } : x)))} />
                  </label>
                  <div className="bulk-photos">
                    {it.photos.map((p) => (
                      <div key={p.key} className="bulk-thumb">
                        <img src={p.url ? mediaUrl(p.url) : p.preview} alt="" />
                        {!p.url && !p.error && <span className="busy"><span className="spinner" /></span>}
                        {p.error && <span className="busy" style={{ color: "var(--bad)", fontSize: 10 }}>!</span>}
                      </div>
                    ))}
                  </div>
                  <input className="input" placeholder="Notes (brand, size, material…) — optional" value={it.notes}
                    aria-label={`Notes for product ${idx + 1}`}
                    onChange={(e) => setItems((all) => all.map((x) => (x.key === it.key ? { ...x, notes: e.target.value } : x)))} />
                  <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove product ${idx + 1}`}
                    onClick={() => setItems((all) => all.filter((x) => x.key !== it.key))}><CloseIcon size={14} /></button>
                </div>
              ))}
            </div>
          </section>
        </div>

        <div className="col">
          <section className="card">
            <label className="field">Batch name
              <input className="input" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />
            </label>
          </section>

          <section className="card">
            <h2>Write the listings in</h2>
            <div style={{ display: "flex", gap: 10 }}>
              <label className="choice"><input type="checkbox" checked={langs.en}
                onChange={(e) => setLangs({ ...langs, en: e.target.checked })} />English</label>
              <label className="choice"><input type="checkbox" checked={langs.ur}
                onChange={(e) => setLangs({ ...langs, ur: e.target.checked })} /><span className="urdu" style={{ lineHeight: 1.4 }}>اردو</span> Urdu</label>
            </div>
          </section>

          <section className="card">
            <h2>Format them for</h2>
            {stores?.length === 0 && <p className="small muted">No stores connected — you’ll get general listings.</p>}
            {stores?.map((s) => (
              <label key={s.id} className="check">
                <input type="checkbox" checked={picked.includes(s.id)} disabled={s.status !== "active"}
                  onChange={(e) => setPicked(e.target.checked ? [...picked, s.id] : picked.filter((x) => x !== s.id))} />
                {PLATFORM_NAMES[s.platform]} — {s.name}
              </label>
            ))}
          </section>

          <section className="card">
            <label className="switch-row">Research online
              <input type="checkbox" role="switch" className="switch" checked={research} onChange={(e) => setResearch(e.target.checked)} />
            </label>
            <p className="small muted">Off by default for batches — it adds an AI call per product.</p>
          </section>

          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {progress ? (
              <div className="card" role="status">
                <strong>{progress.note}</strong>
                <div className="bar"><span style={{ width: `${Math.round((progress.done / progress.total) * 100)}%` }} /></div>
                <span className="small muted">Keep this page open until all are started.</span>
              </div>
            ) : (
              <button type="button" className="btn btn-primary btn-lg btn-block" disabled={!canStart} onClick={start}>
                <SparkIcon />Generate {ready.length || ""} listing{ready.length === 1 ? "" : "s"}
              </button>
            )}
            <span className="small" style={{ textAlign: "center", color: notEnough ? "var(--bad)" : "var(--muted)" }}>
              {uploading ? "Waiting for photos to finish uploading…"
                : !ready.length ? "Add photos to start."
                : credits === 0 ? "You're out of credits — buy a pack to keep generating."
                : credits !== null ? `Uses ${ready.length} credit${ready.length === 1 ? "" : "s"} · you have ${credits}`
                  + (notEnough ? " — only the first ones will be written" : "")
                : `Uses ${ready.length} credits`}
            </span>
          </div>
        </div>
      </div>
    </>
  );
}

function sessionStorageSafe(key: string, value: string) {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

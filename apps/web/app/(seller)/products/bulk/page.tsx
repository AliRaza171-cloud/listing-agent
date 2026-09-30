"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { CloseIcon, MicIcon, SparkIcon, StopIcon, UploadIcon } from "@/components/Icons";
import { ModeSwitch } from "@/components/ModeSwitch";
import { useRecorder } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { myMarket,
  ApiError, createBatch, createProduct, deleteProduct, generateListing, groupPhotos, listStores, mediaUrl, splitNotes,
  transcribe, updateProduct, uploadPhoto, type Store,
} from "@/lib/api";
import { PLATFORM_NAMES } from "@/lib/format";
import { money } from "@/lib/markets";

const MAX_ITEMS = 50;
const MAX_PHOTOS_PER_ITEM = 8;
const MAX_MB = 8;
const TYPES = ["image/jpeg", "image/png", "image/webp"];

type Photo = { key: string; preview: string; url: string | null; error: string | null };
type Item = {
  key: string; photos: Photo[]; notes: string; selected: boolean;
  label: string;                 // the AI's name for the product ("steel water bottle")
  sorted: boolean;               // already looked at by the AI grouping (new photos aren't)
  price: number | null; stock: number | null; discount_pct: number | null;   // from the seller's voice note
};
type Notice = { text: string; undo?: Item[] } | null;

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
  const [autoSort, setAutoSort] = useState(true);
  const [sorting, setSorting] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [talk, setTalk] = useState("");
  const [talkBusy, setTalkBusy] = useState<"listening" | "matching" | null>(null);
  const [talkResult, setTalkResult] = useState<{ text: string; bad?: boolean } | null>(null);
  const itemsRef = useRef<Item[]>([]);
  itemsRef.current = items;

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
      return { key: uid(), photos: [photo], notes: "", selected: false, label: "", sorted: false,
        price: null, stock: null, discount_pct: null };
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
      .map((i) => (i.key === first.key ? { ...i, photos, notes, selected: false, sorted: true } : i)));
  }

  /** The AI got it wrong: every photo of this product becomes its own product again. */
  function split(key: string) {
    setItems((all) => all.flatMap((i) => (i.key !== key ? [i] : i.photos.map((p, n) => ({
      ...i, key: n === 0 ? i.key : uid(), photos: [p], selected: false, sorted: true,
      notes: n === 0 ? i.notes : "", label: n === 0 ? i.label : "", price: n === 0 ? i.price : null,
      stock: n === 0 ? i.stock : null, discount_pct: n === 0 ? i.discount_pct : null,
    })))));
  }

  // New photos (one per product, uploaded, not yet looked at) are sorted by the AI once uploads finish.
  const unsorted = items.filter((i) => !i.sorted && i.photos.length === 1 && i.photos[0].url);
  const stillUploading = items.some((i) => i.photos.some((p) => !p.url && !p.error));
  useEffect(() => {
    if (!autoSort || sorting || stillUploading || progress || unsorted.length === 0) return;
    const keys = unsorted.map((i) => i.key);
    if (keys.length === 1) {
      setItems((all) => all.map((i) => (i.key === keys[0] ? { ...i, sorted: true } : i)));
      return;
    }
    const t = window.setTimeout(() => { void sortWithAI(keys); }, 700);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSort, sorting, stillUploading, progress, unsorted.map((i) => i.key).join(",")]);

  async function sortWithAI(keys: string[]) {
    const before = itemsRef.current;
    const batch = keys.map((k) => before.find((i) => i.key === k)).filter((i): i is Item => !!i && !!i.photos[0]?.url);
    const markSorted = () => setItems((all) => all.map((i) => (keys.includes(i.key) ? { ...i, sorted: true } : i)));
    if (batch.length < 2) { markSorted(); return; }
    setSorting(true);
    setNotice(null);
    try {
      const { groups } = await groupPhotos(batch.map((i) => i.photos[0].url as string));
      setItems((all) => {
        const byKey = new Map(all.map((i) => [i.key, i]));
        const merged = new Map<string, Item>();   // first item's key -> the combined product
        const gone = new Set<string>();
        for (const g of groups) {
          const members = g.photos.map((n) => batch[n]).filter((i) => i && byKey.has(i.key)).map((i) => byKey.get(i.key)!);
          if (!members.length) continue;
          const [first, ...rest] = members;
          rest.forEach((r) => gone.add(r.key));
          merged.set(first.key, {
            ...first, sorted: true, label: g.label || first.label,
            photos: members.flatMap((m) => m.photos).slice(0, MAX_PHOTOS_PER_ITEM),
            notes: members.map((m) => m.notes.trim()).filter(Boolean).join(" "),
            selected: members.some((m) => m.selected),
          });
        }
        return all.filter((i) => !gone.has(i.key)).map((i) => merged.get(i.key) ?? (keys.includes(i.key) ? { ...i, sorted: true } : i));
      });
      const products = groups.length;
      setNotice(products < batch.length
        ? { text: `The AI put ${batch.length} photos into ${products} product${products === 1 ? "" : "s"}. Check them below — tick and Combine, or Split, to fix any.`,
            undo: before.map((i) => (keys.includes(i.key) ? { ...i, sorted: true } : i)) }
        : { text: `The AI found ${products} different products — none of the new photos were combined.` });
    } catch (e) {
      markSorted();
      setNotice({ text: e instanceof ApiError ? e.message : "Couldn't sort the photos — tick and Combine them yourself." });
    } finally {
      setSorting(false);
    }
  }

  /** One message (spoken or typed) about many products -> each product's notes, price and stock. */
  async function applyTalk(text: string) {
    const said = text.trim();
    if (!said) return;
    const snapshot = itemsRef.current.filter((i) => i.photos.some((p) => p.url));
    if (!snapshot.length) {
      setTalkResult({ text: "Add the photos first, then tell the agent about them.", bad: true });
      return;
    }
    setTalkBusy("matching");
    setTalkResult(null);
    try {
      const res = await splitNotes(said, snapshot.map((i) => ({
        label: i.label, image_url: i.photos.find((p) => p.url)?.url ?? null,
      })));
      const updates = new Map(snapshot.map((i, n) => [i.key, res.products[n]]));
      setItems((all) => all.map((i) => {
        const u = updates.get(i.key);
        if (!u || (!u.notes && u.price === null && u.stock === null && u.discount_pct === null)) return i;
        const notes = !u.notes ? i.notes : i.notes.trim() && !i.notes.includes(u.notes) ? `${i.notes.trim()} ${u.notes}` : (i.notes.trim() || u.notes);
        return { ...i, notes, price: u.price ?? i.price, stock: u.stock ?? i.stock, discount_pct: u.discount_pct ?? i.discount_pct };
      }));
      const count = snapshot.filter((i) => {
        const u = updates.get(i.key);
        return u && (u.notes || u.price !== null || u.stock !== null || u.discount_pct !== null);
      }).length;
      setTalk("");
      setTalkResult({
        text: (count ? `Added details to ${count} of ${snapshot.length} product${snapshot.length === 1 ? "" : "s"} — check them below.`
          : "I couldn't tell which product you meant — try naming them (\"the bottle…\", \"the first one…\").")
          + (res.unmatched ? ` Not placed: “${res.unmatched}”` : ""),
        bad: !count,
      });
    } catch (e) {
      setTalk(said);
      setTalkResult({ text: e instanceof ApiError ? e.message : "Couldn't match your details — try again.", bad: true });
    } finally {
      setTalkBusy(null);
    }
  }

  const recorder = useRecorder(async (audio) => {
    setTalkBusy("listening");
    setTalkResult(null);
    try {
      const { text } = await transcribe(audio);
      const full = talk.trim() ? `${talk.trim()} ${text}` : text;
      setTalk(full);
      await applyTalk(full);
    } catch (e) {
      setTalkResult({ text: e instanceof ApiError ? e.message : "Couldn't understand the recording.", bad: true });
    } finally {
      setTalkBusy(null);
    }
  }, 180);

  const uploading = stillUploading;
  const ready = items.filter((i) => i.photos.some((p) => p.url));
  const languages = (["en", "ur"] as const).filter((l) => langs[l]);
  const chosenStores = useMemo(() => (stores ?? []).filter((s) => picked.includes(s.id)), [stores, picked]);
  const notEnough = credits !== null && ready.length > credits;
  const canStart = !progress && !uploading && !sorting && !talkBusy && credits !== 0 && ready.length > 0 && languages.length > 0 && name.trim().length > 0;

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
        const id = (await createProduct(item.photos.filter((p) => p.url).map((p) => p.url as string),
          item.notes.trim() || null, batchId)).id;
        ids.push(id);
        if (item.price !== null || item.stock !== null || item.discount_pct !== null) {
          // What the seller said in the voice note; not fatal if it doesn't save (they can set it later).
          await updateProduct(id, {
            ...(item.price !== null ? { price: item.price } : {}), ...(item.stock !== null ? { stock: item.stock } : {}),
            ...(item.discount_pct !== null ? { discount_pct: item.discount_pct } : {}),
          }).catch(() => failures.push(`Product ${i + 1}: price/stock didn't save — set it on the product.`));
        }
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
          <p>Drop in all your product photos. The AI puts photos of the same product together — then tell it about them in one go.</p>
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
              <span className="small">{autoSort ? "Photos of the same product are combined automatically."
                : "One photo = one product. Several photos of one product? Tick them and press “Combine”."}</span>
              <input ref={fileInput} type="file" accept={TYPES.join(",")} multiple hidden
                onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
            </div>

            <label className="switch-row small">AI combines photos of the same product
              <input type="checkbox" role="switch" className="switch" checked={autoSort} onChange={(e) => setAutoSort(e.target.checked)} />
            </label>
            {sorting && <div className="alert" role="status"><span className="spinner" /> Sorting photos into products…</div>}
            {notice && !sorting && (
              <div className="alert" role="status" style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ flex: 1, minWidth: 200 }}>{notice.text}</span>
                {notice.undo && <button type="button" className="btn btn-ghost btn-sm"
                  onClick={() => { setItems(notice.undo as Item[]); setNotice(null); }}>Undo</button>}
                <button type="button" className="btn btn-ghost btn-sm" aria-label="Dismiss" onClick={() => setNotice(null)}><CloseIcon size={14} /></button>
              </div>
            )}

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
                  <div className="bulk-info">
                    <span className="bulk-label">{idx + 1}. {it.label || (it.photos.length > 1 ? `${it.photos.length} photos` : "Product")}</span>
                    <input className="input" placeholder="What should the agent know? (brand, size…) — optional" value={it.notes}
                      aria-label={`Notes for product ${idx + 1}`}
                      onChange={(e) => setItems((all) => all.map((x) => (x.key === it.key ? { ...x, notes: e.target.value } : x)))} />
                    {(it.price !== null || it.stock !== null || it.discount_pct !== null) && (
                      <span className="small bulk-facts">
                        {[it.price !== null && money(it.price, myMarket().currency), it.discount_pct !== null && `${it.discount_pct}% off`,
                          it.stock !== null && `stock ${it.stock}`].filter(Boolean).join(" · ")}
                        <button type="button" className="link-btn" aria-label={`Clear price and stock for product ${idx + 1}`}
                          onClick={() => setItems((all) => all.map((x) => (x.key === it.key ? { ...x, price: null, stock: null, discount_pct: null } : x)))}>clear</button>
                      </span>
                    )}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {it.photos.length > 1 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => split(it.key)}
                      aria-label={`Split product ${idx + 1} into separate products`}>Split</button>}
                    <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove product ${idx + 1}`}
                      onClick={() => setItems((all) => all.filter((x) => x.key !== it.key))}><CloseIcon size={14} /></button>
                  </div>
                </div>
              ))}
            </div>
          </section>

          {items.length > 0 && (
            <section className="card">
              <label htmlFor="talk" style={{ fontSize: 17, fontWeight: 600 }}>Tell the agent about all of them</label>
              <p className="small muted">
                Speak once — the AI puts each detail on the right product. e.g. “The bottle is 750 ml steel, price
                2800, stock 40. The black wallet is real leather, 1500. All are imported.”
              </p>
              <div className="notes-wrap">
                <textarea id="talk" className="textarea" rows={3} value={talk} onChange={(e) => setTalk(e.target.value)}
                  placeholder="Tap the mic and describe your products — or type here" disabled={!!talkBusy} />
                <button type="button" className={`icon-btn${recorder.recording ? " recording" : ""}`}
                  aria-label={recorder.recording ? "Stop recording" : "Speak about your products"}
                  disabled={!!talkBusy || sorting}
                  onClick={() => (recorder.recording ? recorder.stop() : recorder.start())}>
                  {talkBusy ? <span className="spinner" /> : recorder.recording ? <StopIcon size={16} /> : <MicIcon />}
                </button>
              </div>
              {recorder.recording && <span className="small" style={{ color: "var(--bad)" }}>Listening… {recorder.seconds}s — tap to stop</span>}
              {recorder.error && <span className="small" style={{ color: "var(--bad)" }}>{recorder.error}</span>}
              {talkBusy && <span className="small muted">{talkBusy === "listening" ? "Writing down what you said…" : "Matching details to your products…"}</span>}
              {talkResult && <span className="small" role="status" style={{ color: talkResult.bad ? "var(--bad)" : "var(--ok)" }}>{talkResult.text}</span>}
              {talk.trim() && !talkBusy && (
                <button type="button" className="btn btn-ghost btn-sm" style={{ alignSelf: "flex-start" }} disabled={sorting}
                  onClick={() => applyTalk(talk)}><SparkIcon size={14} />Fill in the products</button>
              )}
            </section>
          )}
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
                : sorting ? "Sorting photos into products…"
                : talkBusy ? "Filling in your details…"
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

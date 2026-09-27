import type { Display } from "@/lib/format";

export default function StatusBadge({ status }: { status: Display }) {
  return <span className={`badge badge-${status.key}`}>{status.label}</span>;
}

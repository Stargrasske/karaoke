/** Item in the mutable play queue exposed via the (Phase 0) /api/queue* endpoints. */
export interface QueueItem {
  index: number;
  artist: string;
  title: string;
  url?: string | null;
  key?: string | null;
  note?: string | null;
}

export interface QueueResponse {
  items: QueueItem[];
  count: number;
}

export interface QueueReorderRequest {
  from_index: number;
  to_index: number;
}

export interface EnqueueRequest {
  artist: string;
  title: string;
  url?: string | null;
}

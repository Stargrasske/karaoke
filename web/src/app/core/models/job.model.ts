export type JobStatus = 'pending' | 'running' | 'done' | 'error';

/** Async job handle returned by (Phase 0) long-running endpoints: staging, folder scan, recording analysis. */
export interface Job {
  job_id: string;
  status: JobStatus;
  progress?: number | null;
  result?: unknown;
  error?: string | null;
}

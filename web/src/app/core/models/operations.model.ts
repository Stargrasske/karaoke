export interface QueueDetails {
  name: string;
  messages: number;
  messages_ready: number;
  messages_unacknowledged: number;
  consumers: number;
}

export interface WorkerUnitDetails {
  unit: string;
  active: string;
  running: boolean;
}

export interface WorkerStatus {
  orchestrator: string;
  available: boolean;
  dashboard_url?: string | null;
  workers_active: number;
  queue_depth: number;
  queue_details: QueueDetails;
  worker_details: WorkerUnitDetails[];
  reason?: string | null;
}

export interface LibraryWorkerStatus {
  available: boolean;
  reason?: string | null;
  queue: {
    name: string;
    ready: number;
    unacked: number;
    queued: number;
    consumers: number;
    deliver_rate: number;
    publish_rate: number;
    busy: boolean;
  };
  workers: {
    count: number;
    running: boolean;
    pids: number[];
    cpu_percent?: number | null;
    rss_mb?: number | null;
  };
}

export interface ErrorLogResponse {
  status: string;
  count?: number;
  logs: string[];
}

export interface LogResponse {
  file: string;
  lines: string[];
}

export interface FolderScanRequest {
  dir: string;
  use_fingerprint?: boolean;
  classify_audio?: boolean;
  resolve_streaming?: boolean;
  dry_run?: boolean;
  limit?: number | null;
}

export interface AudioCutRequest {
  file_path: string;
  start_s?: number;
  duration_s: number;
  output_path?: string | null;
}

export interface WindowRequest {
  state?: 'normal' | 'minimized' | 'maximized' | 'fullscreen' | 'focus';
  left?: number;
  top?: number;
  width?: number;
  height?: number;
}

export interface RecordDevicesResponse {
  devices: string[];
  platform: 'windows' | 'linux';
  capture_backend?: string;
  capture_mode?: string;
  ffmpeg_path?: string | null;
  identification_backend?: string;
}

export interface RecordStatusResponse {
  recording: Array<{
    recording_id: number;
    elapsed_s: number;
    source: string;
    marks: number;
    identified: number;
    audio_bytes: number;
    level_db?: number | null;
    detected_artist?: string | null;
    detected_title?: string | null;
    detected_at?: number | null;
  }>;
  count: number;
}

export interface SampleRequest {
  artist?: string | null;
  title?: string | null;
  seconds?: number | null;
  source?: string | null;
}
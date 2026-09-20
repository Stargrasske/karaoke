export interface LyricLine {
  index: number;
  time: number;
  end?: number | null;
  text: string;
  words?: number[];
}

export interface QueuedTrackRef {
  artist: string;
  title: string;
}

export type PlaybackStatus = 'Playing' | 'Paused' | 'Stopped';

export interface MusicalEpisode {
  kind: 'intro' | 'vocals' | 'instrumental' | 'outro' | 'unknown';
  label: string;
  start?: number | null;
  end?: number | null;
  line_index?: number;
}

/** Snapshot shape emitted ~10Hz over /api/stage/stream and returned once by /api/stage/state. */
export interface PlaybackState {
  artist?: string | null;
  title?: string | null;
  album?: string | null;
  art_url?: string | null;
  duration?: number | null;
  position_s: number;
  status: PlaybackStatus;
  bpm?: number | null;
  key?: string | null;
  energy?: number | null;
  active_line_index: number;
  next_line_in?: number | null;
  episode?: MusicalEpisode;
  lines: LyricLine[];
  upcoming_queue: QueuedTrackRef[];
  casting: boolean;
  timestamp: number;
}

export interface Track {
  track_id: number;
  artist: string;
  title: string;
  album?: string | null;
  duration?: number | null;
  url?: string | null;
  kind?: string | null;
  key?: string | null;
  bpm?: number | null;
  energy?: number | null;
  brightness?: number | null;
  genre?: string | null;
  play_count: number;
  has_synced_lyrics: boolean;
}

export interface Lyrics {
  plain?: string | null;
  synced_raw?: string | null;
  source?: string | null;
  has_synced: boolean;
  line_count: number;
}

export interface TrackDetail extends Track {
  lyrics?: Lyrics;
}

export interface SoundsLikeResult {
  track_id: number;
  artist: string;
  title: string;
  score: number;
  seeds_matched?: number;
  space?: string;
  url?: string | null;
}

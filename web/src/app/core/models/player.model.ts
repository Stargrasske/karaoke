export interface PlaySession {
  session_id: string;
  status: 'launched' | 'stopped';
  url?: string | null;
  kind?: string | null;
  pid?: number | null;
  artist?: string | null;
  title?: string | null;
  prefer_audio: boolean;
  launched_at: number;
  stopped_at?: number | null;
}

export interface MprisPlayerMetadata {
  artist?: string | null;
  title?: string | null;
  album?: string | null;
  url?: string | null;
  player?: string | null;
  mpris_name?: string | null;
  duration?: number | null;
}

export interface MprisPlayer {
  player: string;
  status: PlaybackStatusMpris;
  position_s: number;
  art_url?: string | null;
  metadata: MprisPlayerMetadata | null;
}

export interface PlayersResponse {
  players: string[];
  playing: string[];
  active?: string | null;
  count: number;
}

export interface PlaySessionsResponse {
  sessions: PlaySession[];
  count: number;
}

export type PlaybackStatusMpris = 'Playing' | 'Paused' | 'Stopped';

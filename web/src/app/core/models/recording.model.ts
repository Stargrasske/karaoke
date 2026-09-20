export interface RecordingTrackSegment {
  artist: string;
  title: string;
  start_wall: number;
  end_wall: number;
  duration_s: number;
  marks: number;
  spread_s?: number | null;
  confident: boolean;
  audio_available: boolean;
}

export type RecordingStatus = 'recording' | 'complete' | 'analysed' | 'discarded' | 'failed';

export interface Recording {
  recording_id: number;
  status: RecordingStatus;
  started_at: number;
  ended_at?: number | null;
  source?: string | null;
  keep_audio: boolean;
  note?: string | null;
  marks: number;
  identified: number;
  segment_files: number;
  captured_s: number;
  running: boolean;
  tracks?: RecordingTrackSegment[];
}

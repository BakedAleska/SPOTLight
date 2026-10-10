export interface PathPoint { id: string; name: string; floor: number; x: number; y: number }
export interface PathEvent { id: string; missionId: string; timestamp: string; type: string; actionId?: string; actionType?: string; durationSec?: number; assetName?: string; locationId?: string; battery?: number; error?: string; severity?: string }
export interface PathMission { id: string; name: string; events: PathEvent[] }
export interface PathFeed { items: PathEvent[]; nextCursor: string | null }

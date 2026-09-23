export interface DraftSelectionToken {
  generation: number;
  candidateId: string;
}

export class DraftSelectionGeneration {
  private generation = 0;

  begin(candidateId: string): DraftSelectionToken {
    this.generation += 1;
    return { generation: this.generation, candidateId };
  }

  invalidate(): void {
    this.generation += 1;
  }

  isCurrent(token: DraftSelectionToken, candidateId: string): boolean {
    return token.generation === this.generation
      && token.candidateId === candidateId;
  }
}

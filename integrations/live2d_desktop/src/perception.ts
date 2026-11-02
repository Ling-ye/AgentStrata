// A single latest-state sampler. The backend owns the model interval and run lifecycle.
export class Perception {
  enabled = false;
  visible = false;
  busy = false;
  locked = false;
  private pending = false;
  private generation = 0;
  private active = false;
  private timer?: ReturnType<typeof setInterval>;
  constructor(private capture: () => Promise<string>,
    private publish: (enabled: boolean, hash?: string) => Promise<void>,
    private failed: (error: unknown) => void) {}
  start() { this.timer = setInterval(() => void this.tick(), 1000); }
  async setEnabled(value: boolean) {
    this.enabled = value; this.generation++;
    if (!value) { this.active = false; await this.publish(false); }
  }
  async suspend() {
    this.generation++;
    if (this.active) { this.active = false; await this.publish(false); }
  }
  async tick() {
    if (this.pending) return;
    if (!this.enabled || !this.visible || this.locked) {
      if (this.active) await this.suspend().catch(this.failed);
      return;
    }
    if (this.busy) return;
    this.pending = true;
    const generation = this.generation;
    try {
      const hash = await this.capture();
      if (generation !== this.generation || !this.enabled || !this.visible || this.locked || this.busy) return;
      this.active = true;
      await this.publish(true, hash);
      // A hide/disable racing an in-flight request must win after that request.
      if (generation !== this.generation || !this.enabled || !this.visible || this.locked || this.busy) await this.publish(false);
    } catch (error) {
      this.enabled = false;
      await this.publish(false).catch(() => undefined);
      this.failed(error);
    } finally { this.pending = false; }
  }
  async close() { if (this.timer) clearInterval(this.timer); await this.setEnabled(false); }
}

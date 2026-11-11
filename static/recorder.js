// 마이크 소리를 0.25초씩 모아 화면 쪽으로 넘긴다. 화면은 그대로 서버에 보낸다.
class Recorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(Math.round(sampleRate / 4));
    this.filled = 0;
  }

  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i += 1) {
      this.buffer[this.filled] = input[i];
      this.filled += 1;
      if (this.filled === this.buffer.length) {
        this.port.postMessage(this.buffer.slice().buffer);
        this.filled = 0;
      }
    }
    return true;
  }
}
registerProcessor('recorder', Recorder);

const { contextBridge, ipcRenderer } = require('electron');

const arg = process.argv.find((a) => a.startsWith('--spinbridge-port='));
const port = arg ? Number(arg.split('=')[1]) : 8765;

contextBridge.exposeInMainWorld('spinbridge', {
  port,
  onEngineExit: (cb) => ipcRenderer.on('engine-exit', (_e, code) => cb(code)),
});

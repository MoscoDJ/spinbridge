// SpinBridge: proceso principal de Electron. Arranca el engine (Python) y lo apaga al cerrar.
const { app, BrowserWindow, dialog } = require('electron');
const { spawn, execFileSync } = require('child_process');
const path = require('path');
const net = require('net');
const fs = require('fs');

const PORT = 8765;
let engine = null;
let win = null;
let quitting = false;

if (!app.requestSingleInstanceLock()) {
  app.quit();
}
app.on('second-instance', () => {
  if (win) {
    if (win.isMinimized()) win.restore();
    win.focus();
  }
});

function enginePaths() {
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, 'engine', 'spinbridge-engine.exe');
    return { cmd: exe, args: [], gpuExe: exe };
  }
  // desarrollo: python del sistema con el código fuente
  const script = path.join(__dirname, '..', 'engine', 'server.py');
  return { cmd: 'python', args: ['-u', script], gpuExe: null };
}

// Spout solo comparte la imagen entre procesos que usan la misma GPU. Resolume va en la GPU de alto
// rendimiento, así que el engine se registra igual (Windows lo lee al arrancar el proceso).
function preferHighPerformanceGpu(exe) {
  if (!exe) return;
  try {
    execFileSync('reg', ['add', 'HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences',
      '/v', exe, '/t', 'REG_SZ', '/d', 'GpuPreference=2;', '/f'], { windowsHide: true });
  } catch (e) {
    console.error('No se pudo registrar la preferencia de GPU:', e.message);
  }
}

function portInUse(port) {
  return new Promise((resolve) => {
    const s = net.connect({ port, host: '127.0.0.1' });
    s.once('connect', () => { s.destroy(); resolve(true); });
    s.once('error', () => resolve(false));
  });
}

async function startEngine() {
  if (await portInUse(PORT)) {
    if (app.isPackaged) {
      dialog.showErrorBox('SpinBridge', `El puerto ${PORT} ya está en uso. ¿Hay otro SpinBridge abierto?`);
      app.quit();
      return;
    }
    console.log('Engine ya corriendo (modo desarrollo): me conecto a él');
    return;
  }
  const { cmd, args, gpuExe } = enginePaths();
  preferHighPerformanceGpu(gpuExe);
  const logDir = path.join(app.getPath('appData'), 'SpinBridge');
  fs.mkdirSync(logDir, { recursive: true });
  const out = fs.createWriteStream(path.join(logDir, 'engine-console.log'), { flags: 'a' });
  engine = spawn(cmd, [...args, '--port', String(PORT), '--watch-stdin'], {
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  engine.stdout.pipe(out);
  engine.stderr.pipe(out);
  engine.on('exit', (code) => {
    engine = null;
    if (!quitting && win) {
      win.webContents.send('engine-exit', code);
    }
  });
}

function stopEngine() {
  return new Promise((resolve) => {
    if (!engine) return resolve();
    const proc = engine;
    const timer = setTimeout(() => {
      // no salió por las buenas: matar el árbol completo
      try { execFileSync('taskkill', ['/PID', String(proc.pid), '/T', '/F'], { windowsHide: true }); } catch (e) { /* ya salió */ }
      resolve();
    }, 4000);
    proc.once('exit', () => { clearTimeout(timer); resolve(); });
    // cerrar stdin hace que el engine detenga la proyección, cierre sesiones y salga
    proc.stdin.end();
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: '#0d0f14',
    title: 'SpinBridge',
    autoHideMenuBar: true,
    icon: path.join(__dirname, 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: [`--spinbridge-port=${PORT}`],
    },
  });
  win.setMenu(null);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.on('close', (e) => {
    if (quitting) return;
    const choice = dialog.showMessageBoxSync(win, {
      type: 'warning',
      buttons: ['Cancelar', 'Cerrar SpinBridge'],
      defaultId: 0,
      cancelId: 0,
      title: 'SpinBridge',
      message: '¿Cerrar SpinBridge?',
      detail: 'Se detendrá la proyección en todos los ventiladores.',
    });
    if (choice !== 1) e.preventDefault();
  });
}

app.whenReady().then(async () => {
  await startEngine();
  createWindow();
});

app.on('before-quit', async (e) => {
  if (quitting) return;
  quitting = true;
  e.preventDefault();
  await stopEngine();
  app.exit(0);
});

app.on('window-all-closed', () => app.quit());

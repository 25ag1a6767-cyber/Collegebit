const { spawnSync } = require('node:child_process');

const localEnv = {
  ...process.env,
  PYTHONPATH: 'backend',
  DATABASE_URL: 'sqlite:///./backend/e2e_canteen.db',
  SECRET_KEY: 'local-e2e-only-secret-key-change-this-before-deployment',
  ENVIRONMENT: 'development',
  AUTO_CREATE_TABLES: 'true',
  SEED_DEMO_DATA: 'true',
  RATE_LIMIT_ENABLED: 'false',
  RAZORPAY_KEY_ID: '',
  RAZORPAY_KEY_SECRET: '',
  RAZORPAY_WEBHOOK_SECRET: '',
};

function runPython(args) {
  const command = process.platform === 'win32' ? 'py' : 'python';
  const commandArgs = process.platform === 'win32' ? ['-3', ...args] : args;
  const result = spawnSync(command, commandArgs, {
    cwd: process.cwd(),
    env: localEnv,
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`E2E database setup failed (${result.status}).`);
}

module.exports = async () => {
  if (process.env.BASE_URL) return;
  runPython(['-m', 'alembic', '-c', 'backend/alembic.ini', 'upgrade', 'head']);
  runPython(['e2e/seed_authenticated.py']);
};

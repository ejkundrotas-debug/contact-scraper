// PM2 конфигурация для запуска Streamlit в sandbox (через PM2).
// Используется только для предпросмотра в sandbox-окружении.
// В продакшне (VPS) поднимается через docker-compose.
module.exports = {
  apps: [
    {
      name: 'lead-ai-scraper',
      script: '.venv/bin/streamlit',
      args: [
        'run', 'app.py',
        '--server.address=0.0.0.0',
        '--server.port=3000',
        '--server.headless=true',
        '--browser.gatherUsageStats=false',
        '--server.enableCORS=false',
        '--server.enableXsrfProtection=false'
      ],
      cwd: '/home/user/webapp',
      interpreter: 'none',  // streamlit это shell-bin, не JS
      env: {
        STREAMLIT_BROWSER_GATHER_USAGE_STATS: 'false',
        STREAMLIT_SERVER_HEADLESS: 'true',
        PYTHONUNBUFFERED: '1'
      },
      watch: false,
      instances: 1,
      exec_mode: 'fork',
      max_memory_restart: '1G',
      autorestart: true
    }
  ]
};

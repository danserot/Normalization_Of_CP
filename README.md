# ReadDocument

## AI extraction pipeline

PDF and DOCX files can be processed through the local AI pipeline:

```text
React -> FastAPI /api/extract -> PDF/DOCX text extraction -> Ollama Qwen2.5-VL-7B -> JSON -> React
```

Start the local model:

```bash
ollama pull qwen2.5vl:7b
ollama serve
```

Start the extraction API from the project root:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Then start the frontend with `npm run dev`. PDF/DOCX requests are sent to the API automatically. If the API or Ollama is unavailable, the browser parser remains a fallback.

The API returns the same `CommercialProposal` JSON shape used by React. Set `OLLAMA_MODEL` or `OLLAMA_URL` to use another local Ollama model or host.

## Docker

Run the complete stack (frontend, API, Ollama and Qwen model) with:

```powershell
docker compose up --build
```

Open `http://localhost:8080`. The first startup downloads the Qwen model into the persistent `ollama_data` volume and can take several minutes. The Ollama container needs enough disk space for the model; GPU passthrough can be added to the `ollama` service when Docker Desktop is configured for GPU support.

Stop the stack:

```powershell
docker compose down
```

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])

```

You can also install [eslint-plugin-react-x](https://npmx.dev/package/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://npmx.dev/package/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])

```

# Hive Mind Backlog

## Future Enhancements

### Terminal Interface Improvements
- [ ] Implement rich terminal UI using `rich` or `prompt_toolkit`
  - Colored output for different message types
  - Formatted code blocks
  - Chat history panel with scrolling
  - Status indicators for agent activity
  - Real-time streaming with better formatting

- [ ] Add session history database
  - SQLite or JSON file storage
  - Persist conversations across sessions
  - Search/filter previous conversations
  - Export conversation history (JSON, Markdown)
  - Session tagging and categorization

- [ ] Improve command system
  - Tab completion for commands
  - Command aliases (e.g., `/v` for `/voice`)
  - Configuration file support
  - Customizable keyboard shortcuts

### Core Agent Features
- [ ] Implement skill auto-generation workflow
  - Skill matching engine to detect if skill exists
  - Dynamic tool creation from user requests
  - Runtime registration without restart
  - Skill versioning and deprecation

- [ ] Enhanced message history
  - Token counting for context management
  - Automatic summary of long conversations
  - Memory prioritization (recent messages weighted higher)

### Speech Improvements
- [ ] Add wake word detection for hands-free mode
  - Detect "Hive Mind" or custom trigger words
  - Automatic recording when wake word detected

- [ ] Support multiple TTS voices and languages
  - Voice selection via `/voice-set alloy|echo|fable|onyx|nova|shimmer`
  - Language support via `/language en|es|fr|etc`
  - Speed adjustment `/tts-speed 0.5-2.0`

- [ ] Local speech recognition fallback
  - Use `speech_recognition` library as offline backup
  - Graceful fallback when OpenAI API unavailable
  - Hybrid online/offline mode

- [ ] Audio streaming for faster TTS playback
  - Stream TTS output instead of waiting for full response
  - Parallel processing of text and speech

### Integration Improvements
- [ ] FastAPI file editor enhancements
  - Real-time syntax highlighting
  - Code execution in editor
  - Terminal output within editor UI

- [ ] MCP Server improvements
  - Export conversation history as MCP resource
  - Real-time message streaming to MCP clients
  - Tool execution tracking and logging

### Deployment
- [ ] Docker compose improvements
  - Health checks for all services
  - Volume management for persistence
  - Resource limits and scaling

- [ ] Kubernetes manifests
  - Helm charts for deployment
  - Service mesh integration
  - Scaling policies

### Testing & Quality
- [ ] Unit tests for terminal commands
  - Command parsing and execution
  - Message processing logic
  - Speech service mocking

- [ ] Integration tests
  - End-to-end chat workflows
  - Tool execution verification
  - Speech API integration (with mocking)

- [ ] Documentation
  - User guide for terminal interface
  - API documentation for agent_tooling integration
  - Deployment guide for production use

## Completed
- [x] Remove Gradio web UI
- [x] Create terminal REPL interface
- [x] Integrate OpenAI Whisper (STT) support
- [x] Integrate OpenAI TTS support
- [x] Update Docker configuration
- [x] Update start/stop scripts
- [x] Create BACKLOG for future work

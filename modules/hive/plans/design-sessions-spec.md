# Design sessions — agreed specification

Three console pages (requirements, tests, functions) that a mind populates
and Daniel edits, bound to a live conversation, persisted so the work
survives the conversation.

## Requirements (signed off)

### The list
1. The console has a Design section whose front page lists every design
   session — title, bound conversation, last-touched time.
2. Any design session can be opened from that list; any can be deleted.
3. A design session survives the conversation that created it.
4. Opening a design session shows the latest state of all three pages.

### Starting one
5. The mind creates a design session when Daniel says so in conversation;
   no menu selection.
6. Every page shows which conversation it is attached to.
7. Claiming from a different conversation re-attaches the existing design
   session and the page says which conversation it is now attached to.

### Page one — requirements
8. Numbered requirements, one per row, as the mind understands them.
9. Each requirement's text is editable by Daniel.
10. Each requirement has its own question/statement box.
11. Two buttons: "Submit feedback" and "Accept" (advances to page two).
12. Submit feedback stores Daniel's edited requirements and questions, and
    notifies the mind.
13. When the mind publishes a revision, the page shows revised requirements,
    an answer to each question asked, and empty feedback boxes — on screen
    and in storage.

### Page two — tests
14. Tests the mind proposes, one per row, each naming the requirement it
    protects.
15. Each test shows the real function it imports — actual source pulled live
    off disk, with file path and line span.
16. Each test shows the values fed in and the outcome expected, including at
    least one that succeeds and one that fails.
17. Each test states in one plain sentence which behaviour it protects.
18. A test naming a function that does not exist is marked unresolved and
    page two cannot be advanced.
19. Same editable rows, per-test feedback, "Submit feedback" and "Accept".

### Page three — functions
20. Every function being added or modified, in call order.
21. One unchanged function above each entry point and one unchanged function
    below each leaf.
22. For each: callers, callees, parameters, arguments passed.
23. Per-function feedback box.
24. Two buttons: "Submit feedback" and "Build".
25. Build is the go signal — the mind starts coding with no further chat.

### Everywhere
26. Anything the mind publishes is stored on publish.
27. Anything Daniel submits is stored on submit.
28. Skippy only in this version.
29. Design routes accept either Daniel's browser login or the mind's bearer
    token, and refuse anything else.
30. Daniel's submission is delivered to the bound conversation: typed into
    the tmux pane for a terminal, through comms for a chat surface.
31. A published revision may only change items Daniel gave feedback on;
    untouched items come back byte-identical. Enforced in the store.
32. Requirements and tests are stored as key/value pairs in a JSON column.

### Assumptions (accepted)
- A design session can target more than one repository.
- Changing a requirement after pages two or three are populated marks them
  stale on screen.
- The page refreshes itself when the mind publishes.
- Feedback submitted against a dead conversation is still stored and read
  when next attached.
- The pages do not run tests, execute code, or commit anything.

## Test list (signed off)

1. Creating two design sessions and listing them returns both with title,
   bound conversation and last-touched time, ordered by recency.
2. Deleting removes it from the list; deleting an id the store's own listing
   says is absent is refused, not reported as success.
3. Re-binding returns the same design session with the new conversation and
   all content intact.
4. Publishing requirements then reading back returns them in order.
5. Submitting edited text and a question against requirement 3 stores both;
   read-back shows Daniel's text in place of the mind's.
6. Publishing a revision after a submission returns new text with feedback
   empty in the store.
7. A submission is stored when the bound conversation no longer exists and
   stays pending until delivered.
8. Resolving a function that exists returns its real source, path and line
   span, compared against the same lines read off the file.
9. Resolving a name the file's own symbol listing says is absent is reported
   unresolved, not empty and not fabricated.
10. The same function name in two repositories resolves to the one in the
    repository the design session targets.
11. A tests page with any unresolved function cannot be advanced; with all
    resolved it can.
12. A published test row round-trips requirement ref, inputs, expected
    outcomes and behaviour sentence.
13. The call-graph trim returns every changed function, exactly one unchanged
    caller above each entry and one unchanged callee below each leaf.
14. A branching change set gets a neighbour above each entry and below each
    leaf, not one pair for the whole graph.
15. Changing a requirement after tests are published marks tests stale;
    republishing clears it.
16. Feedback submission and build submission produce different pending
    notices.
17. A design route succeeds with the mind's bearer, refuses 401 with none and
    with a wrong credential.
18. A terminal-bound design session routes its notice to the pane; a
    Telegram-bound one routes through comms.
19. Publishing a revision changing a fed-back item and an untouched item
    stores the first and leaves the second byte-identical.
20. Requirements and tests read back as individually addressable keys.

### Untestable, named
- Button existence and labels — appearance.
- Page self-refresh on publish — browser-side; verified live.
- Skippy-only — configuration.
- Pages not executing or committing — an absence.

"""The tools sreditor's MCP server serves and the operations each groups (rule agentic.mcp.tools.outcomes).

Reads and writes are separate tools, so a client can let an agent read freely and still ask before
it changes anything; the two tools that reach ElevenLabs are apart for the same reason. Every
operation belongs to exactly one tool, which build() checks.
"""

from __future__ import annotations

from sreditor.mcp_groups import ToolGroup

READ_CHANNEL = ToolGroup(
    name="read_channel",
    title="Read channels",
    purpose=(
        "Read channels and what belongs to the whole channel: list_channels and whoami first, "
        "then a channel, its brand kit, Editorial, members, declared and observed profile, its "
        "pitches, what changed since your last cursor, a search across everything it wrote, the "
        "classification vocabulary, and how its posts performed (compare_posts, what_worked, "
        "list_post_snapshots, get_experiment). Changes nothing. For one episode use read_episode; "
        "for characters, places, styles, props and third-party media use read_library."
    ),
    operations=(
        "list_channels",
        "whoami",
        "get_channel",
        "get_brand",
        "get_editorial",
        "list_members",
        "get_channel_profile",
        "get_classification_vocabulary",
        "list_events",
        "search_channel",
        "list_pitches",
        "get_pitch",
        "compare_posts",
        "what_worked",
        "list_post_snapshots",
        "get_experiment",
    ),
)

READ_LIBRARY = ToolGroup(
    name="read_library",
    title="Read the library",
    purpose=(
        "Read a channel's library: characters, locations, visual styles and props with their "
        "srstudio sheets, the relationships between characters, third-party media and the "
        "episodes that use each item, and the context (what, who, where, when, why, how) and "
        "links of any item. Changes nothing; change the library with edit_library."
    ),
    operations=(
        "list_library",
        "get_library_sheet",
        "get_visual_style",
        "list_relationships",
        "list_external_media",
        "list_external_media_episodes",
        "list_linkable_items",
        "get_context",
    ),
)

READ_EPISODE = ToolGroup(
    name="read_episode",
    title="Read episodes",
    purpose=(
        "Read a channel's episodes: list_episodes, then get_episode_overview first for one "
        "episode (a digest of each part with its revision and the readiness list, each gap "
        "naming the operation that fixes it), then any part in full: research, claims, "
        "screenplay, short script, voice-over and its cost estimate, style, media files, "
        "renders, delivery, credits, classification and waivers. Changes nothing; each part's "
        "`revision` is the `expected_revision` the edit tools take."
    ),
    operations=(
        "list_episodes",
        "get_episode",
        "get_episode_overview",
        "get_research",
        "list_claims",
        "get_screenplay",
        "get_short_script",
        "get_voice_over",
        "estimate_voice_over",
        "get_episode_style",
        "list_media",
        "get_render",
        "list_renders",
        "get_delivery",
        "get_episode_credits",
        "get_episode_classification",
        "list_waivers",
    ),
)

MANAGE_CHANNEL = ToolGroup(
    name="manage_channel",
    title="Manage a channel",
    purpose=(
        "Create a channel or change one: name, language and status, platform accounts, brand "
        "kit, Editorial guidelines, declared profile, and members (invite, change a role, "
        "remove). Read the channel with read_channel first; most operations take the revision "
        "it shows as `expected_revision`. Some operations remove for good (remove_account, "
        "retire_guideline, remove_member)."
    ),
    operations=(
        "create_channel",
        "update_channel",
        "set_channel_status",
        "put_account",
        "remove_account",
        "update_brand",
        "write_guideline",
        "retire_guideline",
        "declare_channel_profile",
        "invite_member",
        "change_role",
        "remove_member",
    ),
)

EDIT_PITCHES = ToolGroup(
    name="edit_pitches",
    title="Edit pitches",
    purpose=(
        "Create and change pitches, the channel's story ideas, with their own research notes and "
        "sources; delete one no episode uses. Read them with read_channel (list_pitches, "
        "get_pitch); to pick the pitches an episode uses, use edit_episode (set_episode_pitches)."
    ),
    operations=(
        "create_pitch",
        "update_pitch",
        "write_pitch_notes",
        "add_pitch_source",
        "remove_pitch_source",
        "delete_pitch",
    ),
)

EDIT_LIBRARY = ToolGroup(
    name="edit_library",
    title="Edit the library",
    purpose=(
        "Change a channel's library: save or remove characters, locations, styles and props, "
        "fill their srstudio sheets and a style's data, keep relationships between characters, "
        "add, change or delete third-party media, and write any item's context and links. Read "
        "it with read_library first for ids and revisions."
    ),
    operations=(
        "save_library_entry",
        "remove_library_entry",
        "save_library_sheet",
        "save_visual_style",
        "save_prop",
        "save_relationship",
        "remove_relationship",
        "add_external_media",
        "update_external_media",
        "remove_external_media_copy",
        "delete_external_media",
        "write_context",
        "unlink_items",
    ),
)

EDIT_EPISODE = ToolGroup(
    name="edit_episode",
    title="Edit an episode",
    purpose=(
        "Create, change or delete an episode as a whole: title, status and the rest of its "
        "fields, srstudio slug and seed, visual style, the pitches it is built on, its proposed "
        "classification, and reviews. For research use edit_research, for scripts edit_script, "
        "for media edit_media, for delivery and post numbers publish."
    ),
    operations=(
        "create_episode",
        "update_episode",
        "delete_episode",
        "set_episode_slug",
        "set_episode_style",
        "set_episode_pitches",
        "set_episode_pitch",
        "classify_episode",
        "add_review",
    ),
)

EDIT_RESEARCH = ToolGroup(
    name="edit_research",
    title="Edit research",
    purpose=(
        "Write an episode's research: its notes, the sources it cites (update_source also "
        "edits a pitch's source) and the claims it states, marking which sources were checked. "
        "Read it with read_episode (get_research, list_claims) first."
    ),
    operations=(
        "write_research_notes",
        "add_source",
        "update_source",
        "remove_source",
        "add_claim",
        "update_claim",
        "check_claim_source",
        "remove_claim",
    ),
)

EDIT_SCRIPT = ToolGroup(
    name="edit_script",
    title="Edit scripts",
    purpose=(
        "Replace an episode's Fountain screenplay or its vertical short script, or apply the "
        "measured times of a voice-over take to the short script. Read them with read_episode "
        "first; to speak the short script use voice_over."
    ),
    operations=("write_screenplay", "write_short_script", "apply_take_timings"),
)

EDIT_MEDIA = ToolGroup(
    name="edit_media",
    title="Edit media",
    purpose=(
        "Rename or remove an episode's media files, record, change or drop each use of a "
        "third-party item, and cancel a render. Upload files with files (create_upload_link); "
        "list them with read_episode (list_media)."
    ),
    operations=(
        "update_media",
        "remove_media",
        "use_external_media",
        "update_external_media_use",
        "remove_external_media_use",
        "cancel_render",
    ),
)

PUBLISH = ToolGroup(
    name="publish",
    title="Publish and measure",
    purpose=(
        "Deliver an episode and measure it: write its delivery package (cuts and posts), record "
        "a post's link, enter a post's numbers or import its retention CSV, and plan, start or "
        "read an A/B experiment between two episodes. Compare results with read_channel "
        "(compare_posts, what_worked)."
    ),
    operations=(
        "write_delivery",
        "record_post",
        "record_post_metrics",
        "import_retention_csv",
        "save_experiment",
    ),
)

VOICE_OVER = ToolGroup(
    name="voice_over",
    title="Voice-over with ElevenLabs",
    purpose=(
        "Reach ElevenLabs: list its premade voices, or generate an episode's voice-over from its "
        "short script, which spends credits. See read_episode (estimate_voice_over) for the cost "
        "first and read the timed words with read_episode (get_voice_over) after."
    ),
    operations=("list_voices", "generate_voice_over"),
)

RENDER = ToolGroup(
    name="render",
    title="Render an episode",
    purpose=(
        "Queue an episode's render into video; it answers at once with the job, or lists what "
        "the episode still lacks. Follow it with read_episode (get_render) until it is done, "
        "then download the video with files (get_file_link); cancel with edit_media."
    ),
    operations=("request_render",),
)

FILES = ToolGroup(
    name="files",
    title="Move files",
    purpose=(
        "Get single-use links valid 15 minutes: one to download a brand file, media file, "
        "render, third-party copy, srstudio export, episode data or channel book, or one to "
        "upload an episode media file, third-party copy or brand file; and check what an "
        "srstudio export still lacks. Waivers never travel through links."
    ),
    operations=("get_file_link", "create_upload_link", "check_srstudio_export"),
)

GROUPS: tuple[ToolGroup, ...] = (
    READ_CHANNEL,
    READ_LIBRARY,
    READ_EPISODE,
    MANAGE_CHANNEL,
    EDIT_PITCHES,
    EDIT_LIBRARY,
    EDIT_EPISODE,
    EDIT_RESEARCH,
    EDIT_SCRIPT,
    EDIT_MEDIA,
    PUBLISH,
    VOICE_OVER,
    RENDER,
    FILES,
)

# Which tool serves each operation, for messages and tests that start from an operation's name.
OPERATION_TOOL: dict[str, str] = {
    operation: group.name for group in GROUPS for operation in group.operations
}

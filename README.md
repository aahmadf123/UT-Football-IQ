# Football-IQ

Football-IQ is a local American-football film room and computer-vision platform
for the Toledo Rockets. Import practice or game film from any angle or height,
group multiple recordings into a session, review plays, and correct observations.
Processing defaults to the computer's CPU; cloud GPUs are optional external
infrastructure, not a dependency.

Film review works without inference models. A configured real player detector
adds candidate tracks; football events, identity, field measurements, and tactical
labels have explicit review and validation limits. Human corrections can train a
local coverage candidate, with independent evaluation and administrator promotion
before serving. The repository does not establish Hudl/SkillCorner accuracy or
automatic tactical tagging on arbitrary footage.


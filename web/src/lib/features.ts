export function dashboardFeatures() {
  const flagDetails = process.env.FEATURE_FLAG_DETAILS !== "false";
  return {
    flagDetails,
    flagActions: flagDetails && process.env.FEATURE_FLAG_ACTIONS !== "false",
    flagExplanations: flagDetails && process.env.FEATURE_FLAG_EXPLANATIONS !== "false",
  };
}

/* Kept separate from dashboardFeatures() so the dashboard payload (and its
   tests) don't change shape. FEATURE_AI_ASSISTANT=false makes every
   /api/chat route 404 and the /assistant page show a disabled notice. */
export function assistantFeatures() {
  return { aiAssistant: process.env.FEATURE_AI_ASSISTANT !== "false" };
}

export function dashboardFeatures() {
  const flagDetails = process.env.FEATURE_FLAG_DETAILS !== "false";
  return {
    flagDetails,
    flagActions: flagDetails && process.env.FEATURE_FLAG_ACTIONS !== "false",
    flagExplanations: flagDetails && process.env.FEATURE_FLAG_EXPLANATIONS !== "false",
  };
}

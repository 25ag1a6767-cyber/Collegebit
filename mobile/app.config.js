module.exports = ({ config }) => {
  const configured =
    (process.env.EXPO_PUBLIC_API_URL || process.env.API_BASE_URL || (config.extra && config.extra.apiBaseUrl) || "")
      .trim()
      .replace(/\/+$/, "");
  const easProjectId =
    (process.env.EXPO_PUBLIC_EAS_PROJECT_ID || (config.extra && config.extra.eas && config.extra.eas.projectId) || "").trim();

  return {
    ...config,
    extra: {
      ...(config.extra || {}),
      apiBaseUrl: configured,
      ...(easProjectId ? { eas: { ...((config.extra && config.extra.eas) || {}), projectId: easProjectId } } : {}),
    },
  };
};

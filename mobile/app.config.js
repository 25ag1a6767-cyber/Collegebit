module.exports = ({ config }) => {
  const configured =
    (process.env.EXPO_PUBLIC_API_URL || process.env.API_BASE_URL || (config.extra && config.extra.apiBaseUrl) || "")
      .trim()
      .replace(/\/+$/, "");

  return {
    ...config,
    extra: {
      ...(config.extra || {}),
      apiBaseUrl: configured,
    },
  };
};

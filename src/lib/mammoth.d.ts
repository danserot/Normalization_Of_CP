declare module "mammoth" {
  export function extractRawText(input: {
    arrayBuffer: ArrayBuffer;
  }): Promise<{ value: string }>;
  export function convertToHtml(input: {
    arrayBuffer: ArrayBuffer;
  }): Promise<{ value: string }>;
}
